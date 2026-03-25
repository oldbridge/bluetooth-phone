# Copyright 2019 by Xabier Zubizarreta.
# All rights reserved.
# This file is released under the "MIT License Agreement".
# More information on this license can be read under https://opensource.org/licenses/MIT

import RPi.GPIO as GPIO
import dbus
import alsaaudio
import yaml
import logging

from pathlib import Path
import time
import wave
import queue
from threading import Event, Lock, Thread
from subprocess import call


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
                )
                stream.setchannels(wav_file.getnchannels())
                stream.setrate(wav_file.getframerate())

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

    def close(self):
        self.stop()


class PhoneManager(object):
    POLL_INTERVAL_SECONDS = 0.5

    def __init__(self, audio_player, asset_dir):
        """
        The PhoneManager class manages the calls and the communication with the ofono service.
        """
        self.audio_player = audio_player
        self.asset_dir = Path(asset_dir)
        self.bus = dbus.SystemBus()
        self.voice_call_manager = None
        self.call_in_progress = False
        self.incoming_call = False
        self.on_incoming_call_changed = None
        self.available = False

        logging.getLogger("dbus.proxies").setLevel(logging.WARNING)

        try:
            manager = dbus.Interface(self.bus.get_object('org.ofono', '/'), 'org.ofono.Manager')
            modems = manager.GetModems()
        except dbus.exceptions.DBusException as exc:
            self._report_init_error(exc)
            return

        if not modems:
            print("ofono is running but no modem is available")
            return

        # Take the first modem (there should be actually only one in our case)
        modem = modems[0][0]
        print(modem)
        self.org_ofono_obj = self.bus.get_object('org.ofono', modem)
        self.voice_call_manager = dbus.Interface(self.org_ofono_obj, 'org.ofono.VoiceCallManager')

        self.available = True
        has_call, has_incoming = self._get_call_info()
        self.call_in_progress = has_call
        self.incoming_call = has_incoming
        self._stop_event = Event()
        self._monitor_thread = Thread(target=self._monitor_calls, daemon=True)
        self._monitor_thread.start()
        print("Initialized")

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
            calls = self.voice_call_manager.GetCalls()
            if not calls:
                return False, False
            states = {str(props.get('State', '')) for _, props in calls}
            has_incoming = 'incoming' in states
            return True, has_incoming
        except dbus.exceptions.DBusException:
            return False, False

    def _set_call_state(self, in_progress):
        if in_progress == self.call_in_progress:
            return
        self.call_in_progress = in_progress
        if in_progress:
            print("Call in progress!")
        else:
            print("Call ended!")

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
        self.voice_call_manager.HangupAll()
        self._set_call_state(False)
        self._set_incoming_state(False)

    def answer_call(self):
        """Answer an incoming call."""
        if not self.available or self.voice_call_manager is None:
            return
        try:
            calls = self.voice_call_manager.GetCalls()
            for path, props in calls:
                if str(props.get('State', '')) == 'incoming':
                    call_obj = self.bus.get_object('org.ofono', path)
                    call_iface = dbus.Interface(call_obj, 'org.ofono.VoiceCall')
                    call_iface.Answer()
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
        try:
            for candidate in candidates:
                for hide_id_option in hide_id_candidates:
                    try:
                        print("Dialing via oFono: %s (hide_id=%r)" % (candidate, hide_id_option))
                        self.voice_call_manager.Dial(candidate, hide_id_option)
                        self._set_call_state(True)
                        return
                    except dbus.exceptions.DBusException as e:
                        if e.get_dbus_name() != 'org.ofono.Error.InvalidFormat':
                            raise
            print("Invalid dialed number format!")
            self.audio_player.play(self._asset_path("format_incorrect.wav"))
        except dbus.exceptions.DBusException as e:
            name = e.get_dbus_name()
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
        self._ring_stop_event = Event()
        self._ring_lock = Lock()
        self._ringer_io_lock = Lock()
        self._ringer_test_active = Event()
        self._ring_thread = None
        self.phone_manager.on_incoming_call_changed = self._on_incoming_call_changed
        self.rotary_dial = RotaryDial(num_pin, self.number_q)
        self.finish = False
        self.receiver_down = self._is_receiver_down()
        self._manual_number = ''
        self._last_digit_at = None
        self._dial_complete_pause = 5.0
        self._min_lifted_digits_to_call = 3
        self._lifted_queue_timeout = 0.2

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
        return GPIO.input(self.receiver_pin) == GPIO.HIGH

    def _clear_manual_dial_state(self):
        self._manual_number = ''
        self._last_digit_at = None

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
        if self.receiver_down:
            self._clear_manual_dial_state()
            self._stop_ringing()
            if self.phone_manager.call_in_progress or self.phone_manager.incoming_call:
                self.phone_manager.end_call()
            self.stop_file()
            return
        self._stop_ringing()
        if self.phone_manager.incoming_call:
            self.phone_manager.answer_call()
            return
        self.start_file(self.asset_dir / "dial_tone.wav", loop=True)

    def receiver_changed(self, pin_num):
        """
        Event triggered when the receiver is hung of lifted.
        :param pin_num: GPIO pin triggering the event (Can only be self.receiver_pin here)
        :return:
        """
        del pin_num
        new_state = self._is_receiver_down()
        if new_state == self.receiver_down:
            return
        self.receiver_down = new_state
        self._apply_receiver_state()

    def start_file(self, filename, loop=False):
        """
        Start a thread reproducing an audio file
        :param filename: The name of the file to play
        :param loop: If the file should be played as a loop (like in the case of the dial tone)
        """
        self.audio_player.play(filename, loop=loop)

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
                        call("sudo shutdown -h now", shell=True)
                    elif c == 5:
                        self.ringer_test()
                    elif 1 <= c <= len(self.phonebook):
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
