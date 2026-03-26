# Copyright 2019 by Xabier Zubizarreta.
# All rights reserved.
# This file is released under the "MIT License Agreement".
# More information on this license can be read under https://opensource.org/licenses/MIT

import RPi.GPIO as GPIO
import dbus
import alsaaudio
import yaml
import logging
import math
import re
import struct

from pathlib import Path
import time
import wave
import queue
from threading import Event, Lock, Thread
import subprocess


class PhonebookLoader(yaml.SafeLoader):
    """YAML loader keeping phone numbers like +34... as strings."""


# Do not auto-cast integers, otherwise +346... loses the leading plus sign.
for key, resolvers in list(PhonebookLoader.yaml_implicit_resolvers.items()):
    PhonebookLoader.yaml_implicit_resolvers[key] = [
        (tag, regexp) for tag, regexp in resolvers if tag != 'tag:yaml.org,2002:int'
    ]


class RotaryDial(Thread):
    """
    Thread class reading the dialed values and putting them into a thread queue
    """

    def __init__(self, ns_pin, number_queue):
        super().__init__(daemon=True)
        self.pin = ns_pin
        self.number_q = number_queue
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self.value = 0
        self.pulse_threshold = 0.2
        self.poll_interval = 0.002
        self.debounce_seconds = 0.09
        self._stop_event = Event()
        self._value_lock = Lock()
        self._last_pulse_at = 0.0
        self._uses_event_detect = False
        self._last_state = GPIO.input(self.pin)
        self._last_fall_at = 0.0

        try:
            GPIO.remove_event_detect(self.pin)
        except RuntimeError:
            pass

        try:
            GPIO.add_event_detect(self.pin, GPIO.FALLING, callback=self._increment, bouncetime=90)
            self._uses_event_detect = True
        except RuntimeError as exc:
            # Some kernels/drivers do not allow edge detection on this pin;
            # keep working by sampling the pin directly in the thread loop.
            print("Rotary GPIO event detect unavailable on pin %d, using polling (%s)" % (self.pin, exc))

    def _increment(self, pin_num):
        """
        Increment function trigered each time a falling pulse is detected.
        :param pin_num: GPIO pin triggering the event (Can only be self.ns_pin here)
        """
        del pin_num
        with self._value_lock:
            self.value += 1
            self._last_pulse_at = time.monotonic()

    def _poll_pin(self):
        current_state = GPIO.input(self.pin)
        now = time.monotonic()

        if self._last_state == GPIO.HIGH and current_state == GPIO.LOW:
            if now - self._last_fall_at >= self.debounce_seconds:
                self._increment(self.pin)
                self._last_fall_at = now

        self._last_state = current_state

    def run(self):
        while not self._stop_event.is_set():
            if not self._uses_event_detect:
                self._poll_pin()
                time.sleep(self.poll_interval)

            with self._value_lock:
                if self.value == 0:
                    continue
                if time.monotonic() - self._last_pulse_at < self.pulse_threshold:
                    continue

                dialed_value = self.value
                self.value = 0

            self.number_q.put(0 if dialed_value == 10 else dialed_value)

    def stop(self):
        self._stop_event.set()
        try:
            if self._uses_event_detect:
                GPIO.remove_event_detect(self.pin)
        except RuntimeError:
            pass


class AudioPlayer(object):
    """
    Single-threaded WAV player shared by the telephone and the phone manager.
    """

    def __init__(self, chunk_size=1024):
        self.chunk_size = chunk_size
        self._lock = Lock()
        self._thread = None
        self._stop_event = None
        self._playback_id = 0

    @property
    def is_playing(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def play(self, filename, loop=False):
        self.stop()
        stop_event = Event()
        with self._lock:
            self._playback_id += 1
            playback_id = self._playback_id
            self._stop_event = stop_event
            self._thread = Thread(
                target=self._play_file,
                args=(Path(filename), loop, stop_event, playback_id),
                daemon=True,
            )
            thread = self._thread
        thread.start()

    def play_tone_pattern(self, frequency_hz=450.0, on_ms=125, off_ms=375, sample_rate=8000):
        self.stop()
        stop_event = Event()
        with self._lock:
            self._playback_id += 1
            playback_id = self._playback_id
            self._stop_event = stop_event
            self._thread = Thread(
                target=self._play_tone_pattern,
                args=(frequency_hz, on_ms, off_ms, sample_rate, stop_event, playback_id),
                daemon=True,
            )
            thread = self._thread
        thread.start()

    def stop(self):
        with self._lock:
            self._playback_id += 1
            stop_event = self._stop_event
            thread = self._thread
            self._stop_event = None
            self._thread = None

        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1)

    def _play_file(self, filename, loop, stop_event, playback_id):
        stream = None
        try:
            with wave.open(str(filename), "rb") as wav_file:
                stream = alsaaudio.PCM(
                    type=alsaaudio.PCM_PLAYBACK,
                    mode=alsaaudio.PCM_NORMAL,
                    channels=wav_file.getnchannels(),
                    rate=wav_file.getframerate(),
                )

                while not stop_event.is_set():
                    data = wav_file.readframes(self.chunk_size)
                    if data:
                        stream.write(data)
                        continue
                    if not loop:
                        break
                    wav_file.rewind()
        except Exception as exc:
            print("Audio playback failed for %s: %s" % (filename, exc))
        finally:
            if stream is not None:
                del stream
            with self._lock:
                if playback_id == self._playback_id:
                    self._thread = None
                    self._stop_event = None

    def _play_tone_pattern(self, frequency_hz, on_ms, off_ms, sample_rate, stop_event, playback_id):
        stream = None
        try:
            stream = alsaaudio.PCM(
                type=alsaaudio.PCM_PLAYBACK,
                mode=alsaaudio.PCM_NORMAL,
                channels=1,
                rate=sample_rate,
                format=alsaaudio.PCM_FORMAT_S16_LE,
            )

            on_frames = max(1, int(sample_rate * (on_ms / 1000.0)))
            off_frames = max(0, int(sample_rate * (off_ms / 1000.0)))

            amplitude = int(32767 * 0.3)
            phase_step = (2.0 * math.pi * frequency_hz) / sample_rate

            on_buffer = bytearray()
            for i in range(on_frames):
                sample = int(amplitude * math.sin(phase_step * i))
                on_buffer.extend(struct.pack('<h', sample))

            off_buffer = b'\x00\x00' * off_frames if off_frames > 0 else b''

            while not stop_event.is_set():
                stream.write(on_buffer)
                if stop_event.is_set():
                    break
                if off_buffer:
                    stream.write(off_buffer)
        except Exception as exc:
            print("Tone playback failed: %s" % exc)
        finally:
            if stream is not None:
                del stream
            with self._lock:
                if playback_id == self._playback_id:
                    self._thread = None
                    self._stop_event = None

    def close(self):
        self.stop()


class UplinkBridge(object):
    """Capture USB mic and pipe to BlueALSA SCO via arecord | aplay subprocesses.

    Using subprocesses instead of a Python-level ALSA loop eliminates GIL
    contention and prevents indefinite stalls caused by blocking PCM writes
    when the Bluetooth SCO buffer is congested (critical on low-power hardware).
    """

    _BACKOFF_INITIAL = 0.2
    _BACKOFF_MAX = 8.0

    def __init__(self, bt_device=None, capture_device='plughw:Device,0', sample_rate=8000, channels=1, period_frames=160):
        self.bt_device = None
        self.capture_device = capture_device
        self.playback_device = None
        self.sample_rate = sample_rate
        self.channels = channels
        self.period_frames = period_frames
        self._stop_event = Event()
        self._thread = None
        self._lock = Lock()
        self._proc_lock = Lock()
        self._rec_proc = None
        self._play_proc = None
        self.set_bt_device(bt_device)

    def set_bt_device(self, bt_device):
        with self._lock:
            self.bt_device = str(bt_device).strip() if bt_device else None
            if self.bt_device:
                self.playback_device = "bluealsa:DEV=%s,PROFILE=sco" % self.bt_device
            else:
                self.playback_device = None

    @property
    def is_running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self):
        if not self.playback_device:
            print("[UPLINK] No Bluetooth device configured, not starting")
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = Thread(target=self._run, daemon=True)
            thread = self._thread
        print("[UPLINK] Starting subprocess ALSA bridge")
        thread.start()

    def stop(self):
        self._stop_event.set()
        self._terminate_procs()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        if thread is not None and thread.is_alive():
            print("[UPLINK] Bridge thread did not stop within timeout")
            return
        with self._lock:
            if self._thread is thread:
                self._thread = None
        print("[UPLINK] Stopped subprocess ALSA bridge")

    def _terminate_procs(self):
        with self._proc_lock:
            rec, play = self._rec_proc, self._play_proc
            self._rec_proc = None
            self._play_proc = None
        for proc in (rec, play):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _wait_for_sco_available(self):
        """Return True once the BlueALSA SCO PCM device can be opened, False if stopped."""
        while not self._stop_event.is_set():
            try:
                pcm = alsaaudio.PCM(
                    type=alsaaudio.PCM_PLAYBACK,
                    mode=alsaaudio.PCM_NONBLOCK,
                    device=self.playback_device,
                    channels=self.channels,
                    rate=self.sample_rate,
                    format=alsaaudio.PCM_FORMAT_S16_LE,
                    periodsize=self.period_frames,
                )
                del pcm
                print("[UPLINK] BlueALSA SCO available")
                return True
            except alsaaudio.ALSAAudioError:
                time.sleep(0.2)
        return False

    def _run(self):
        backoff = self._BACKOFF_INITIAL
        while not self._stop_event.is_set():
            if not self._wait_for_sco_available():
                break

            rec_proc = None
            play_proc = None
            try:
                rec_proc = subprocess.Popen(
                    [
                        'arecord',
                        '-D', self.capture_device,
                        '-f', 'S16_LE',
                        '-r', str(self.sample_rate),
                        '-c', str(self.channels),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                play_proc = subprocess.Popen(
                    [
                        'aplay',
                        '-D', self.playback_device,
                        '-f', 'S16_LE',
                        '-r', str(self.sample_rate),
                        '-c', str(self.channels),
                    ],
                    stdin=rec_proc.stdout,
                    stderr=subprocess.DEVNULL,
                )
                # Close parent copy so rec_proc receives SIGPIPE if play_proc exits.
                rec_proc.stdout.close()

                with self._proc_lock:
                    self._rec_proc = rec_proc
                    self._play_proc = play_proc

                backoff = self._BACKOFF_INITIAL
                while not self._stop_event.is_set():
                    if play_proc.poll() is not None:
                        print("[UPLINK] aplay exited (rc=%d), reconnecting..." % play_proc.returncode)
                        break
                    if rec_proc.poll() is not None:
                        print("[UPLINK] arecord exited (rc=%d), reconnecting..." % rec_proc.returncode)
                        break
                    time.sleep(0.3)

            except Exception as exc:
                print("[UPLINK] Bridge error (%s), reconnecting..." % exc)

            finally:
                self._terminate_procs()
                rec_proc = None
                play_proc = None
                if not self._stop_event.is_set():
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self._BACKOFF_MAX)

        with self._lock:
            self._thread = None


class DownlinkBridge(object):
    """Capture BlueALSA SCO and play to USB audio device (phone speaker)."""

    def __init__(self, bt_device=None, playback_device='plughw:Device,0', sample_rate=8000, channels=1, period_frames=120, on_sco_ready=None):
        self.bt_device = None
        self.capture_device = None
        self.playback_device = playback_device
        self.sample_rate = sample_rate
        self.channels = channels
        self.period_frames = period_frames
        self.on_sco_ready = on_sco_ready
        self._stop_event = Event()
        self._thread = None
        self._lock = Lock()
        self.set_bt_device(bt_device)

    def set_bt_device(self, bt_device):
        with self._lock:
            self.bt_device = str(bt_device).strip() if bt_device else None
            if self.bt_device:
                self.capture_device = "bluealsa:DEV=%s,PROFILE=sco" % self.bt_device
            else:
                self.capture_device = None

    @property
    def is_running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self):
        if not self.capture_device:
            print("[DOWNLINK] No Bluetooth device configured, not starting")
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = Thread(target=self._run, daemon=True)
            thread = self._thread
        print("[DOWNLINK] Starting Python ALSA bridge")
        thread.start()

    def stop(self):
        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        if thread is not None and thread.is_alive():
            print("[DOWNLINK] Bridge thread did not stop within timeout")
            return
        with self._lock:
            if self._thread is thread:
                self._thread = None
        print("[DOWNLINK] Stopped Python ALSA bridge")

    def _create_pcm(self, pcm_type, device):
        mode = alsaaudio.PCM_NONBLOCK if pcm_type == alsaaudio.PCM_CAPTURE else alsaaudio.PCM_NORMAL
        return alsaaudio.PCM(
            type=pcm_type,
            mode=mode,
            device=device,
            channels=self.channels,
            rate=self.sample_rate,
            format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=self.period_frames,
        )

    def _wait_for_capture_ready(self):
        while not self._stop_event.is_set():
            capture = None
            try:
                capture = self._create_pcm(alsaaudio.PCM_CAPTURE, self.capture_device)
                print("[DOWNLINK] BlueALSA capture ready")
                return capture
            except alsaaudio.ALSAAudioError:
                if capture is not None:
                    del capture
                time.sleep(0.2)
        return None

    def _run(self):
        while not self._stop_event.is_set():
            capture = self._wait_for_capture_ready()
            if capture is None:
                return

            if self.on_sco_ready is not None:
                self.on_sco_ready()

            playback = None
            try:
                playback = self._create_pcm(alsaaudio.PCM_PLAYBACK, self.playback_device)
                while not self._stop_event.is_set():
                    frames, data = capture.read()
                    if frames <= 0 or not data:
                        time.sleep(0.01)
                        continue
                    playback.write(data)
            except alsaaudio.ALSAAudioError as exc:
                print("[DOWNLINK] ALSA stream reset (%s), reconnecting..." % exc)
                time.sleep(0.2)
            finally:
                if capture is not None:
                    del capture
                if playback is not None:
                    del playback
        with self._lock:
            self._thread = None


class PhoneManager(object):
    POLL_INTERVAL_SECONDS = 0.5
    DBUS_TIMEOUT_SECONDS = 8

    def __init__(self, audio_player, asset_dir):
        """
        The PhoneManager class manages the calls and the communication with the ofono service.
        """
        self.audio_player = audio_player
        self.asset_dir = Path(asset_dir)
        self.bus = dbus.SystemBus()
        self.voice_call_manager = None
        self.modem_path = None
        self.bt_device_path = None
        self.call_in_progress = False
        self.incoming_call = False
        self.on_incoming_call_changed = None
        self.on_call_started = None
        self.on_call_ended = None
        self.available = False

        logging.getLogger("dbus.proxies").setLevel(logging.WARNING)

        self._manager = None

        try:
            self._manager = dbus.Interface(self.bus.get_object('org.ofono', '/'), 'org.ofono.Manager')
            modems = self._manager.GetModems()
        except dbus.exceptions.DBusException as exc:
            self._report_init_error(exc)
            return

        if not modems:
            print("ofono is running but no modem is available")
            return

        self._bind_best_modem(modems)

        if self.voice_call_manager is None:
            print("No usable oFono modem found")
            return

        self.available = True
        has_call, has_incoming = self._get_call_info()
        self.call_in_progress = has_call
        self.incoming_call = has_incoming
        self._stop_event = Event()
        self._monitor_thread = Thread(target=self._monitor_calls, daemon=True)
        self._monitor_thread.start()
        print("Initialized")

    def _modem_to_bt_path(self, modem_path):
        marker = '/org/bluez/'
        marker_idx = modem_path.find(marker)
        if marker_idx >= 0:
            return modem_path[marker_idx:]
        return None

    def _list_bluez_devices(self):
        try:
            object_manager = dbus.Interface(
                self.bus.get_object('org.bluez', '/'),
                'org.freedesktop.DBus.ObjectManager',
            )
            managed_objects = object_manager.GetManagedObjects()
        except dbus.exceptions.DBusException:
            return []

        devices = []
        for path, ifaces in managed_objects.items():
            device = ifaces.get('org.bluez.Device1')
            if not device:
                continue
            devices.append({
                'path': str(path),
                'address': str(device.get('Address', '')),
                'alias': str(device.get('Alias', 'unknown')),
                'paired': bool(device.get('Paired', False)),
                'connected': bool(device.get('Connected', False)),
                'blocked': bool(device.get('Blocked', False)),
            })
        return devices

    def _modem_supports_voice_calls(self, modem_path):
        try:
            ofono_obj = self.bus.get_object('org.ofono', modem_path)
            voice_call_manager = dbus.Interface(ofono_obj, 'org.ofono.VoiceCallManager')
            # Probe a lightweight call. If the method is missing, this modem is stale.
            voice_call_manager.GetCalls(timeout=self.DBUS_TIMEOUT_SECONDS)
            return True
        except dbus.exceptions.DBusException as exc:
            return exc.get_dbus_name() != 'org.freedesktop.DBus.Error.UnknownMethod'

    def _bind_best_modem(self, modems):
        bluez_devices = self._list_bluez_devices()
        connected_paths = {
            d['path'] for d in bluez_devices
            if d['paired'] and d['connected'] and not d['blocked']
        }
        paired_paths = {
            d['path'] for d in bluez_devices
            if d['paired'] and not d['blocked']
        }

        best = None
        best_score = None

        for idx, (path, _) in enumerate(modems):
            modem_path = str(path)
            bt_path = self._modem_to_bt_path(modem_path)
            supports_voice = self._modem_supports_voice_calls(modem_path)
            score = (
                1 if bt_path in connected_paths else 0,
                1 if supports_voice else 0,
                1 if bt_path in paired_paths else 0,
                -idx,
            )
            print("[OFONO] Candidate modem=%s bt_path=%s voice=%s score=%s" % (
                modem_path,
                bt_path,
                supports_voice,
                score,
            ))
            if best is None or score > best_score:
                best = modem_path
                best_score = score

        if best is None:
            return False

        self.modem_path = best
        self.bt_device_path = self._modem_to_bt_path(best)
        print("[OFONO] Selected modem %s" % self.modem_path)
        self.org_ofono_obj = self.bus.get_object('org.ofono', self.modem_path)
        self.voice_call_manager = dbus.Interface(self.org_ofono_obj, 'org.ofono.VoiceCallManager')
        return True

    def _rebind_modem(self):
        if self._manager is None:
            return False
        try:
            modems = self._manager.GetModems()
        except dbus.exceptions.DBusException as exc:
            print("[OFONO] Failed to refresh modems: %s" % exc)
            return False
        if not modems:
            return False
        return self._bind_best_modem(modems)

    def _report_init_error(self, exc):
        name = exc.get_dbus_name()
        print("Cannot access ofono over D-Bus: %s" % name)
        if name == 'org.freedesktop.DBus.Error.AccessDenied':
            print("Permission denied: add the runtime user to the 'ofono' group and restart the session.")
            print("Example: sudo usermod -aG ofono $USER")
        else:
            print(str(exc))

    def _asset_path(self, filename):
        return self.asset_dir / filename

    def _get_call_info(self):
        """Returns (has_any_call, has_incoming_call)."""
        if self.voice_call_manager is None:
            return False, False
        try:
            calls = self.voice_call_manager.GetCalls(timeout=self.DBUS_TIMEOUT_SECONDS)
            if not calls:
                return False, False
            states = {str(props.get('State', '')) for _, props in calls}
            has_incoming = 'incoming' in states
            return True, has_incoming
        except dbus.exceptions.DBusException:
            return False, False

    def _disconnect_bt_device(self):
        if not self.bt_device_path:
            return
        try:
            dev_obj = self.bus.get_object('org.bluez', self.bt_device_path)
            dev_iface = dbus.Interface(dev_obj, 'org.bluez.Device1')
            dev_iface.Disconnect()
            print("[BT] Forced disconnect on %s to recover HFP state" % self.bt_device_path)
        except dbus.exceptions.DBusException as exc:
            print("[BT] Forced disconnect failed: %s" % exc)

    def _hangup_all_calls(self):
        try:
            self.voice_call_manager.HangupAll(timeout=self.DBUS_TIMEOUT_SECONDS)
            return
        except dbus.exceptions.DBusException as exc:
            if exc.get_dbus_name() != 'org.freedesktop.DBus.Error.UnknownMethod':
                raise

        try:
            calls = self.voice_call_manager.GetCalls(timeout=self.DBUS_TIMEOUT_SECONDS)
        except dbus.exceptions.DBusException as exc:
            if exc.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownMethod':
                # HFP voice-call methods are not published yet (e.g. before SLC).
                return
            raise

        for path, _ in calls:
            call_obj = self.bus.get_object('org.ofono', path)
            call_iface = dbus.Interface(call_obj, 'org.ofono.VoiceCall')
            call_iface.Hangup(timeout=self.DBUS_TIMEOUT_SECONDS)

    def _set_call_state(self, in_progress):
        if in_progress == self.call_in_progress:
            return
        self.call_in_progress = in_progress
        if in_progress:
            print("Call in progress!")
            if self.on_call_started is not None:
                self.on_call_started()
        else:
            print("Call ended!")
            if self.on_call_ended is not None:
                self.on_call_ended()

    def _set_incoming_state(self, is_incoming):
        if is_incoming == self.incoming_call:
            return
        self.incoming_call = is_incoming
        if is_incoming:
            print("Incoming call!")
        else:
            print("Incoming call ended or answered.")
        if self.on_incoming_call_changed is not None:
            self.on_incoming_call_changed(is_incoming)

    def _monitor_calls(self):
        while not self._stop_event.wait(self.POLL_INTERVAL_SECONDS):
            has_call, has_incoming = self._get_call_info()
            self._set_call_state(has_call)
            self._set_incoming_state(has_incoming)

    def end_call(self):
        """
        Method to finalize the current (all, actually) call
        """
        if not self.available or self.voice_call_manager is None:
            return
        try:
            self._hangup_all_calls()
            self._set_call_state(False)
            self._set_incoming_state(False)
        except dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name()
            # oFono may transiently reject hangup while another call operation is active.
            # Treat this as non-fatal so the main runtime loop keeps running.
            if name == 'org.ofono.Error.InProgress':
                print("Hangup in progress, keeping service alive")
                self._disconnect_bt_device()
                return
            if name == 'org.freedesktop.DBus.Error.NoReply':
                print("Hangup timed out, forcing HFP reconnect")
                self._disconnect_bt_device()
                return
            print("Failed to hang up call: %s" % exc)

    def answer_call(self):
        """Answer an incoming call."""
        if not self.available or self.voice_call_manager is None:
            return
        try:
            calls = self.voice_call_manager.GetCalls(timeout=self.DBUS_TIMEOUT_SECONDS)
            for path, props in calls:
                if str(props.get('State', '')) == 'incoming':
                    call_obj = self.bus.get_object('org.ofono', path)
                    call_iface = dbus.Interface(call_obj, 'org.ofono.VoiceCall')
                    call_iface.Answer(timeout=self.DBUS_TIMEOUT_SECONDS)
                    self._set_call_state(True)
                    self._set_incoming_state(False)
                    return
        except dbus.exceptions.DBusException as e:
            print("Failed to answer call: %s" % e)

    def _normalize_number(self, number):
        # Keep only digits and one optional leading plus for oFono dial.
        raw = str(number).strip()
        if not raw:
            return ''

        has_plus = raw.startswith('+')
        digits = ''.join(ch for ch in raw if ch.isdigit())
        if not digits:
            return ''
        return ('+' if has_plus else '') + digits

    def get_bt_device_address(self):
        """Return Bluetooth address of the currently connected paired device.
        
        First tries the modem's associated device, then falls back to querying
        BlueZ directly for any connected+paired device. This handles device
        switches where oFono's cached path may not match the active device.
        """
        # Try modem-associated device first
        if self.bt_device_path:
            try:
                dev_obj = self.bus.get_object('org.bluez', self.bt_device_path)
                props_iface = dbus.Interface(dev_obj, 'org.freedesktop.DBus.Properties')
                address = str(props_iface.Get('org.bluez.Device1', 'Address'))
                connected = bool(props_iface.Get('org.bluez.Device1', 'Connected'))
                if address and connected:
                    return address
            except dbus.exceptions.DBusException:
                pass
        
        # Fallback: query BlueZ for ANY connected+paired device
        try:
            object_manager = dbus.Interface(
                self.bus.get_object('org.bluez', '/'),
                'org.freedesktop.DBus.ObjectManager',
            )
            managed_objects = object_manager.GetManagedObjects()
            for path, ifaces in managed_objects.items():
                device = ifaces.get('org.bluez.Device1')
                if not device:
                    continue
                
                paired = bool(device.get('Paired', False))
                connected = bool(device.get('Connected', False))
                blocked = bool(device.get('Blocked', False))
                address = str(device.get('Address', ''))
                
                if paired and connected and not blocked and address:
                    return address
        except dbus.exceptions.DBusException:
            pass
        
        return None

    def _dial_candidates(self, normalized_number):
        candidates = [normalized_number]
        if normalized_number.startswith('+'):
            national = normalized_number[1:]
            candidates.append(national)
            candidates.append('00' + national)
        elif normalized_number.startswith('00') and len(normalized_number) > 2:
            candidates.append('+' + normalized_number[2:])

        # Preserve order while dropping duplicates/empty values.
        unique = []
        for candidate in candidates:
            if candidate and candidate not in unique:
                unique.append(candidate)
        return unique

    def call(self, number, hide_id='default'):
        """
        Method to place call. It handles incorrectly dialed numbers thanks to ofono exceptions
        """
        if not self.available or self.voice_call_manager is None:
            print("Call system not available")
            self.audio_player.play(self._asset_path("not_connected.wav"))
            return

        normalized_number = self._normalize_number(number)
        if not normalized_number:
            print("Invalid dialed number format!")
            self.audio_player.play(self._asset_path("format_incorrect.wav"))
            return

        candidates = self._dial_candidates(normalized_number)
        hide_id_candidates = [hide_id, 'default', '']
        hide_id_candidates = [h for i, h in enumerate(hide_id_candidates) if h not in hide_id_candidates[:i]]

        last_error = None
        for attempt in range(2):
            try:
                for candidate in candidates:
                    for hide_id_option in hide_id_candidates:
                        try:
                            print("Dialing via oFono modem=%s: %s (hide_id=%r)" % (
                                self.modem_path,
                                candidate,
                                hide_id_option,
                            ))
                            self.voice_call_manager.Dial(
                                candidate,
                                hide_id_option,
                                timeout=self.DBUS_TIMEOUT_SECONDS,
                            )
                            self._set_call_state(True)
                            return
                        except dbus.exceptions.DBusException as e:
                            if e.get_dbus_name() != 'org.ofono.Error.InvalidFormat':
                                raise
                print("Invalid dialed number format!")
                self.audio_player.play(self._asset_path("format_incorrect.wav"))
                return
            except dbus.exceptions.DBusException as e:
                last_error = e
                if e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownMethod' and attempt == 0:
                    print("[OFONO] Dial method missing on modem %s, refreshing modem binding..." % self.modem_path)
                    if self._rebind_modem():
                        continue
                break

        if last_error is None:
            return
        name = last_error.get_dbus_name()
        if name == 'org.freedesktop.DBus.Error.UnknownMethod':
            print("Ofono not running")
            self.audio_player.play(self._asset_path("not_connected.wav"))
        else:
            print(name)

    def close(self):
        if not self.available:
            return
        self._stop_event.set()
        if self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=1)

    def has_paired_device(self, require_connected=True):
        """Return True when at least one usable BlueZ device is present."""
        try:
            object_manager = dbus.Interface(
                self.bus.get_object('org.bluez', '/'),
                'org.freedesktop.DBus.ObjectManager',
            )
            managed_objects = object_manager.GetManagedObjects()
            found = False
            for path, ifaces in managed_objects.items():
                device = ifaces.get('org.bluez.Device1')
                if not device:
                    continue

                paired = bool(device.get('Paired', False))
                connected = bool(device.get('Connected', False))
                blocked = bool(device.get('Blocked', False))
                alias = str(device.get('Alias', 'unknown'))
                address = str(device.get('Address', path))
                print("[BT] Device %s (%s): paired=%s connected=%s blocked=%s" % (
                    alias,
                    address,
                    paired,
                    connected,
                    blocked,
                ))

                if not paired or blocked:
                    continue
                # BlueZ Connected can be false while oFono/HFP is still usable.
                # If oFono already exposes a modem, accept paired devices.
                if require_connected and not connected and not self.modem_path:
                    continue

                found = True
                break

            print("[BT] Usable device present=%s (require_connected=%s, modem_path=%s)" % (
                found,
                require_connected,
                self.modem_path,
            ))
            return found
        except dbus.exceptions.DBusException as exc:
            print("Cannot query BlueZ paired devices: %s" % exc)
            return False


class Telephone(object):
    """
    Main Telephone class containing everything required for the Bluetooth telephone to work.
    """
    def __init__(self, num_pin, receiver_pin):
        GPIO.setmode(GPIO.BCM)
        self.asset_dir = Path(__file__).resolve().parent
        self.receiver_pin = receiver_pin
        self.ringer_pin = 18
        GPIO.setup(self.receiver_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self.ringer_pin, GPIO.OUT, initial=GPIO.HIGH)
        self.number_q = queue.Queue()
        self.audio_player = AudioPlayer()
        self.phone_manager = PhoneManager(self.audio_player, self.asset_dir)
        modem_bt_device = self.phone_manager.get_bt_device_address()
        if modem_bt_device:
            print("[BT] Using Bluetooth device %s" % modem_bt_device)
        else:
            print("[BT] No connected paired Bluetooth device found yet")
        self.uplink_bridge = UplinkBridge(bt_device=modem_bt_device, capture_device='plughw:Device,0')
        self.downlink_bridge = DownlinkBridge(
            bt_device=modem_bt_device,
            playback_device='plughw:Device,0',
            on_sco_ready=self._on_sco_ready,
        )
        self._ring_stop_event = Event()
        self._ring_lock = Lock()
        self._ringer_io_lock = Lock()
        self._ringer_test_active = Event()
        self._ring_thread = None
        self.phone_manager.on_incoming_call_changed = self._on_incoming_call_changed
        self.phone_manager.on_call_started = self._on_call_started
        self.phone_manager.on_call_ended = self._on_call_ended
        self.rotary_dial = RotaryDial(num_pin, self.number_q)
        self.finish = False
        self._last_receiver_raw_state = None
        self.receiver_down = self._is_receiver_down()
        self._manual_number = ''
        self._last_digit_at = None
        self._dial_complete_pause = 5.0
        self._min_lifted_digits_to_call = 3
        self._lifted_queue_timeout = 0.2
        self._wifi_iface = 'wlan0'
        self._wifi_restore_needed = False
        self._wifi_lock = Lock()

        # Load fast_dial numbers
        with (self.asset_dir / "phonebook.yaml").open('r') as stream:
            self.phonebook = yaml.load(stream, Loader=PhonebookLoader) or []

        print(self.phonebook)

        # Receiver relevant functions
        self._apply_receiver_state()
        self._receiver_event_detect = False
        self._queue_timeout = 5
        try:
            GPIO.remove_event_detect(self.receiver_pin)
        except RuntimeError:
            pass
        try:
            GPIO.add_event_detect(self.receiver_pin, GPIO.BOTH, callback=self.receiver_changed, bouncetime=10)
            self._receiver_event_detect = True
        except RuntimeError as exc:
            print("Receiver GPIO event detect unavailable on pin %d, using polling (%s)" % (self.receiver_pin, exc))
            self._queue_timeout = 0.2

        # Start all threads
        self.rotary_dial.start()

        # Anounce ready
        self.start_file(self.asset_dir / "ready.wav")

    def _is_receiver_down(self):
        raw_state = GPIO.input(self.receiver_pin)
        is_down = raw_state == GPIO.HIGH
        if raw_state != self._last_receiver_raw_state:
            print("[HOOK] Read pin %d: raw=%d -> receiver_%s" % (
                self.receiver_pin,
                raw_state,
                'down' if is_down else 'up',
            ))
            self._last_receiver_raw_state = raw_state
        return is_down

    def _clear_manual_dial_state(self):
        self._manual_number = ''
        self._last_digit_at = None

    def _on_sco_ready(self):
        """Called by the downlink bridge once the SCO link is confirmed active."""
        self.audio_player.stop()
        self.uplink_bridge.start()

    def _refresh_bridge_bt_device(self):
        """Refresh bridge device before starting a call.
        
        This ensures we use the currently connected device, not oFono's
        cached device from initialization. Critical when switching between
        multiple paired phones.
        """
        modem_bt_device = self.phone_manager.get_bt_device_address()
        if not modem_bt_device:
            print("[BT] Cannot refresh bridge device: no connected paired device available")
            return False
        self.uplink_bridge.set_bt_device(modem_bt_device)
        self.downlink_bridge.set_bt_device(modem_bt_device)
        print("[BT] Bridge device refreshed to %s" % modem_bt_device)
        return True

    def _on_call_started(self):
        """Called when a call becomes active. Start only the downlink bridge.

        The uplink is started later via _on_sco_ready, once the downlink has
        confirmed the SCO link is established. Starting the uplink before that
        point causes arecord to buffer audio data against a not-yet-active SCO
        channel, resulting in several seconds of delay at call start.
        """
        self._refresh_bridge_bt_device()
        self._disable_wifi_for_call()
        self.downlink_bridge.start()

    def _on_call_ended(self):
        """Called when an active call ends. Stop the SCO audio bridges."""
        self.uplink_bridge.stop()
        self.downlink_bridge.stop()
        self._restore_wifi_after_call()
        if not self.receiver_down:
            self.start_dial_tone()

    def _run_command_with_sudo_fallback(self, command):
        try:
            if subprocess.call(command) == 0:
                return True
            return subprocess.call(['sudo'] + command) == 0
        except OSError:
            return False

    def _is_wifi_enabled(self):
        operstate_path = Path('/sys/class/net') / self._wifi_iface / 'operstate'
        try:
            state = operstate_path.read_text().strip().lower()
            return state != 'down'
        except OSError:
            return None

    def _set_wifi_enabled(self, enabled):
        rfkill_cmd = ['rfkill', 'unblock' if enabled else 'block', 'wifi']
        if self._run_command_with_sudo_fallback(rfkill_cmd):
            return True

        ip_cmd = ['ip', 'link', 'set', self._wifi_iface, 'up' if enabled else 'down']
        return self._run_command_with_sudo_fallback(ip_cmd)

    def _disable_wifi_for_call(self):
        with self._wifi_lock:
            if self._wifi_restore_needed:
                return

            wifi_enabled = self._is_wifi_enabled()
            if wifi_enabled is False:
                print("[WIFI] %s already disabled before call" % self._wifi_iface)
                return

            if self._set_wifi_enabled(False):
                self._wifi_restore_needed = True
                print("[WIFI] Disabled %s for call" % self._wifi_iface)
            else:
                print("[WIFI] Failed to disable %s for call" % self._wifi_iface)

    def _restore_wifi_after_call(self):
        with self._wifi_lock:
            if not self._wifi_restore_needed:
                return

            if self._set_wifi_enabled(True):
                self._wifi_restore_needed = False
                print("[WIFI] Restored %s after call" % self._wifi_iface)
            else:
                print("[WIFI] Failed to restore %s after call" % self._wifi_iface)

    def _on_incoming_call_changed(self, is_incoming):
        """Called from the PhoneManager monitor thread when incoming call state changes."""
        if self._ringer_test_active.is_set():
            return
        if is_incoming and self.receiver_down:
            self._start_ringing()
        else:
            self._stop_ringing()

    def _start_ringing(self):
        with self._ring_lock:
            if self._ring_thread is not None and self._ring_thread.is_alive():
                return
            self._ring_stop_event.clear()
            self._ring_thread = Thread(target=self._ring_pattern, daemon=True)
            self._ring_thread.start()

    def _stop_ringing(self):
        with self._ring_lock:
            self._ring_stop_event.set()
            thread = self._ring_thread
            self._ring_thread = None
        if not self._ringer_test_active.is_set():
            self._set_ringer(False)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _ring_pattern(self):
        """Ring pattern: 1s ON, 4s OFF, repeating."""
        while not self._ring_stop_event.is_set():
            self._set_ringer(True)
            if self._ring_stop_event.wait(1.0):
                break
            self._set_ringer(False)
            self._ring_stop_event.wait(4.0)
        self._set_ringer(False)

    def _apply_receiver_state(self):
        print("[HOOK] Applying state: receiver_%s" % ('down' if self.receiver_down else 'up'))
        if self.receiver_down:
            self.uplink_bridge.stop()
            self.downlink_bridge.stop()
            self._clear_manual_dial_state()
            self._stop_ringing()
            # Always attempt hangup when placing the handset down. Relying on
            # polled state flags can miss races and leave a call active.
            self.phone_manager.end_call()
            self.stop_file()
            return
        self._stop_ringing()
        if self.phone_manager.incoming_call:
            self.phone_manager.answer_call()
            return
        if self.phone_manager.call_in_progress:
            # A call was placed while the receiver was down (shortcut dial).
            # Bridges are already running via on_call_started; nothing more to do.
            return
        has_paired_device = self.phone_manager.has_paired_device(require_connected=True)
        if not self.phone_manager.available or not has_paired_device:
            print("System not available for dialing (ofono_available=%s, paired_and_connected_device=%s)" % (
                self.phone_manager.available,
                has_paired_device,
            ))
            self.start_busy_tone()
            return
        self.start_dial_tone()

    def receiver_changed(self, pin_num):
        """
        Event triggered when the receiver is hung of lifted.
        :param pin_num: GPIO pin triggering the event (Can only be self.receiver_pin here)
        :return:
        """
        print("[HOOK] GPIO callback on pin %d" % pin_num)
        new_state = self._is_receiver_down()
        if new_state == self.receiver_down:
            print("[HOOK] Callback ignored (no state change): still receiver_%s" % (
                'down' if self.receiver_down else 'up',
            ))
            return
        print("[HOOK] Callback state change: receiver_%s -> receiver_%s" % (
            'down' if self.receiver_down else 'up',
            'down' if new_state else 'up',
        ))
        self.receiver_down = new_state
        self._apply_receiver_state()

    def start_file(self, filename, loop=False):
        """
        Start a thread reproducing an audio file
        :param filename: The name of the file to play
        :param loop: If the file should be played as a loop (like in the case of the dial tone)
        """
        self.audio_player.play(filename, loop=loop)

    def start_busy_tone(self):
        # Besetztton cadence: 450 Hz, 125 ms ON / 375 ms OFF.
        self.audio_player.play_tone_pattern(frequency_hz=450.0, on_ms=125, off_ms=375)

    def start_dial_tone(self):
        # Dial tone: continuous 450 Hz.
        self.audio_player.play_tone_pattern(frequency_hz=450.0, on_ms=100, off_ms=0)

    def stop_file(self):
        self.audio_player.stop()

    def _set_ringer(self, enabled):
        # This relay/generator is active-low: LOW rings, HIGH is silent.
        with self._ringer_io_lock:
            GPIO.output(self.ringer_pin, GPIO.LOW if enabled else GPIO.HIGH)

    def ringer_test(self):
        print("Ringer test: start")
        self._stop_ringing()
        self._ringer_test_active.set()
        try:
            # 2x classic ring cadence: 1s ON, 1s OFF.
            for _ in range(2):
                self._set_ringer(True)
                time.sleep(1)
                self._set_ringer(False)
                time.sleep(1)
        finally:
            self._ringer_test_active.clear()
            self._set_ringer(False)
        print("Ringer test: done")

    def dialing_handler(self):
        """
        Main function of the telephone that handles the dialing if the receiver is lifted or hooked.
        :return:
        """
        while not self.finish:
            if not self._receiver_event_detect:
                new_state = self._is_receiver_down()
                if new_state != self.receiver_down:
                    print("[HOOK] Polling state change: receiver_%s -> receiver_%s" % (
                        'down' if self.receiver_down else 'up',
                        'down' if new_state else 'up',
                    ))
                    self.receiver_down = new_state
                    self._apply_receiver_state()

            if not self.receiver_down:  # Handling of the dialing when the receiver is lifted
                try:
                    c = self.number_q.get(timeout=self._lifted_queue_timeout)
                    self._manual_number += str(c)
                    self._last_digit_at = time.monotonic()
                except queue.Empty:
                    pass

                if self._manual_number and self._last_digit_at is not None:
                    # Rotary-style: wait for a pause between pulses before placing the call.
                    if len(self._manual_number) < self._min_lifted_digits_to_call:
                        continue
                    if time.monotonic() - self._last_digit_at >= self._dial_complete_pause:
                        print("Dialing: %s" % self._manual_number)
                        self.stop_file()
                        self.phone_manager.call(self._manual_number)
                        self._clear_manual_dial_state()

            else:  # Handling of the dialing when the receiver is down
                self._clear_manual_dial_state()
                if self.audio_player.is_playing:
                    self.stop_file()
                try:
                    c = self.number_q.get(timeout=self._queue_timeout)
                    print("Selected %d" % c)
                    if c == 9:
                        print("Turning system off")
                        self.start_file(self.asset_dir / "turnoff.wav")
                        time.sleep(6)
                        subprocess.call("sudo shutdown -h now", shell=True)
                    elif c == 5:
                        self.ringer_test()
                    elif 1 <= c <= len(self.phonebook):
                        if self.phone_manager.call_in_progress or self.phone_manager.incoming_call:
                            print("Call already active/incoming, skipping shortcut dial")
                            self.start_busy_tone()
                            continue
                        print("Shortcut action %d: Automatic dial" % c)
                        shortcut_number = str(self.phonebook[c - 1].get('number', '')).strip()
                        if not shortcut_number:
                            print("Invalid phonebook number for shortcut %d" % c)
                            self.start_file(self.asset_dir / "format_incorrect.wav")
                            continue
                        print(shortcut_number)
                        time.sleep(4)
                        self.phone_manager.call(shortcut_number)
                except queue.Empty:
                    pass

    def close(self):
        self.finish = True
        self.uplink_bridge.stop()
        self.downlink_bridge.stop()
        self._restore_wifi_after_call()
        self._stop_ringing()
        self._set_ringer(False)
        self.rotary_dial.stop()
        if self.rotary_dial.is_alive():
            self.rotary_dial.join(timeout=1)
        self.phone_manager.close()
        self.audio_player.close()
        # Do not cleanup ringer_pin so it stays HIGH (silent) after process exit.
        GPIO.cleanup((self.receiver_pin, self.rotary_dial.pin))


if __name__ == '__main__':
    HOERER_PIN = 13
    NS_PIN = 19

    t = Telephone(NS_PIN, HOERER_PIN)
    try:
        t.dialing_handler()
    except KeyboardInterrupt:
        pass
    t.close()
