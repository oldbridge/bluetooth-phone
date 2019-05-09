# Copyright 2019 by Xabier Zubizarreta.
# All rights reserved.
# This file is released under the "MIT License Agreement".
# More information on this license can be read under https://opensource.org/licenses/MIT

import RPi.GPIO as GPIO
import datetime
import dbus
import dbus.mainloop.glib
import wave
import alsaaudio
import yaml
from gi.repository import GLib

import time
from threading import Thread
from threading import Event
import queue as Queue
import numpy as np
import struct
from subprocess import call


class RotaryDial(Thread):
    """
    Thread class reading the dialed values and putting them into a thread queue
    """

    def __init__(self, ns_pin, number_queue):
        Thread.__init__(self)
        self.pin = ns_pin
        self.number_q = number_queue
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self.value = 0
        self.pulse_threshold = 0.2
        self.finish = False
        GPIO.add_event_detect(ns_pin, GPIO.FALLING, callback=self.__increment, bouncetime=90)

    def __increment(self, pin_num):
        """
        Increment function trigered each time a falling pulse is detected.
        :param pin_num: GPIO pin triggering the event (Can only be self.ns_pin here)
        """
        self.value += 1

    def run(self):
        while not self.finish:
            last_value = self.value
            time.sleep(self.pulse_threshold)
            if last_value != self.value:
                pass
            elif self.value != 0:
                if self.value == 10:
                    self.number_q.put(0)
                else:
                    self.number_q.put(self.value)
                self.value = 0


class PhoneManager(object):
    CHUNK = 1024

    def __init__(self):
        """
        The PhoneManager class manages the calls and the communication with the ofono service.
        """
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        manager = dbus.Interface(bus.get_object('org.ofono', '/'), 'org.ofono.Manager')
        modems = manager.GetModems()

        # Take the first modem (there should be actually only one in our case)
        modem = modems[0][0]
        print(modem)
        self.org_ofono_obj = bus.get_object('org.ofono', modem)
        self.voice_call_manager = dbus.Interface(self.org_ofono_obj, 'org.ofono.VoiceCallManager')

        self.call_in_progress = False
        self._setup_dbus_loop()
        print("Initialized")

    def _setup_dbus_loop(self):
        """
        Sets up the D-Bus signals.
        """
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.loop = GLib.MainLoop()
        self._thread = Thread(target=self.loop.run)
        self._thread.start()

        self.org_ofono_obj.connect_to_signal("CallAdded", self.set_call_in_progress,
                                             dbus_interface='org.ofono.VoiceCallManager')

        self.org_ofono_obj.connect_to_signal("CallRemoved", self.set_call_ended,
                                             dbus_interface='org.ofono.VoiceCallManager')

    def set_call_in_progress(self, object, properties):
        """
        Event triggered when a call is initiated.
        :param object: The address of the call object from ofono
        :param properties: Properties of the call
        :return:
        """
        print("Call in progress!")
        self.call_in_progress = True

    def set_call_ended(self, object):
        """
        Event triggered when a call is ended
        :param object: The address of the call object from ofono (just as reference, cannot be fetched anymore)
        :return:
        """
        print("Call ended!")
        self.call_in_progress = False

    def end_call(self):
        """
        Method to finalize the current (all, actually) call
        """
        self.voice_call_manager.HangupAll()

    def call(self, number, hide_id='default'):
        """
        Method to place call. It handles incorrectly dialed numbers thanks to ofono exceptions
        """
        try:
            self.voice_call_manager.Dial(str(number), hide_id)
        except dbus.exceptions.DBusException as e:
            name = e.get_dbus_name()
            if name == 'org.freedesktop.DBus.Error.UnknownMethod':
                print("Ofono not running")
                self.start_file("/home/pi/telefonoa/not_connected.wav")
            elif name == 'org.ofono.Error.InvalidFormat':
                print("Invalid dialed number format!")
                self.start_file("/home/pi/telefonoa/format_incorrect.wav")
            else:
                print(name)

    def start_file(self, filename, loop=False):
        """
        Start a thread reproducing an audio file
        :param filename: The name of the file to play
        :param loop: If the file should be played as a loop (like in the case of the dial tone)
        """
        self._thread = Thread(target=self.__play_file, args=[filename, loop])
        self._thread.start()
        self.playing_audio = True

    def __play_file(self, filename, loop):
        """
        Private function handling the wav file replay
        :param filename: The name of the file to play
        :param loop: If the file should be played as a loop (like in the case of the dial tone)
        """
        self.stop_audio = False
        if not loop:
            # open a wav format music
            f = wave.open(filename, "rb")
            # open stream
            stream = alsaaudio.PCM(type=alsaaudio.PCM_PLAYBACK,
                                   mode=alsaaudio.PCM_NORMAL)
            stream.setchannels(f.getnchannels())
            stream.setrate(f.getframerate())
            # read data
            data = f.readframes(self.CHUNK)

            # play stream
            while data and not self.stop_audio:
                stream.write(data)
                data = f.readframes(self.CHUNK)
        else:
            # open a wav format music
            f = wave.open(filename, "rb")
            # open stream
            stream = alsaaudio.PCM(type=alsaaudio.PCM_PLAYBACK,
                                   mode=alsaaudio.PCM_NORMAL)
            stream.setchannels(f.getnchannels())
            stream.setrate(f.getframerate())
            # read data
            data = f.readframes(self.CHUNK)

            # play stream
            while loop and not self.stop_audio:
                f.rewind()
                data = f.readframes(self.CHUNK)
                while data and not self.stop_audio:
                    stream.write(data)
                    data = f.readframes(self.CHUNK)


class Telephone(object):
    """
    Main Telephone class containing everything required for the Bluetooth telephone to work.
    """
    CHUNK = 1024

    def __init__(self, num_pin, receiver_pin):
        GPIO.setmode(GPIO.BCM)
        self.receiver_pin = receiver_pin
        self.number_q = Queue.Queue()
        self.phone_manager = PhoneManager()
        self.rotary_dial = RotaryDial(num_pin, self.number_q)
        self.stop_audio = False
        self.playing_audio = False
        self.finish = False

        # Load fast_dial numbers
        with open("phonebook.yaml", 'r') as stream:
            self.phonebook = yaml.safe_load(stream)

        print(self.phonebook)

        # Receiver relevant functions
        GPIO.setup(self.receiver_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        if GPIO.input(self.receiver_pin) is GPIO.HIGH:
            self.receiver_down = False
        else:
            self.receiver_down = True
        self.receiver_changed(self.receiver_pin)
        GPIO.add_event_detect(self.receiver_pin, GPIO.BOTH, callback=self.receiver_changed, bouncetime=10)

        # Start all threads
        self.rotary_dial.start()

        # Anounce ready
        self.start_file("/home/pi/telefonoa/ready.wav")

    def receiver_changed(self, pin_num):
        """
        Event triggered when the receiver is hung of lifted.
        :param pin_num: GPIO pin triggering the event (Can only be self.receiver_pin here)
        :return:
        """
        if self.receiver_down:
            self.receiver_down = False
            self.start_file("/home/pi/telefonoa/dial_tone.wav", loop=True)
        else:
            if self.phone_manager.call_in_progress:
                self.phone_manager.end_call()
            self.receiver_down = True
            self.stop_file()

    def start_file(self, filename, loop=False):
        """
        Start a thread reproducing an audio file
        :param filename: The name of the file to play
        :param loop: If the file should be played as a loop (like in the case of the dial tone)
        """
        self._thread = Thread(target=self.__play_file, args=[filename, loop])
        self._thread.start()
        self.playing_audio = True

    def __play_file(self, filename, loop):
        """
        Private function handling the wav file replay
        :param filename: The name of the file to play
        :param loop: If the file should be played as a loop (like in the case of the dial tone)
        """
        self.stop_audio = False
        if not loop:
            # open a wav format music
            f = wave.open(filename, "rb")
            # open stream
            stream = alsaaudio.PCM(type=alsaaudio.PCM_PLAYBACK,
                                   mode=alsaaudio.PCM_NORMAL)
            stream.setchannels(f.getnchannels())
            stream.setrate(f.getframerate())
            # read data
            data = f.readframes(self.CHUNK)

            # play stream
            while data and not self.stop_audio:
                stream.write(data)
                data = f.readframes(self.CHUNK)
        else:
            # open a wav format music
            f = wave.open(filename, "rb")
            # open stream
            stream = alsaaudio.PCM(type=alsaaudio.PCM_PLAYBACK,
                                   mode=alsaaudio.PCM_NORMAL)
            stream.setchannels(f.getnchannels())
            stream.setrate(f.getframerate())
            # read data
            data = f.readframes(self.CHUNK)

            # play stream
            while loop and not self.stop_audio:
                f.rewind()
                data = f.readframes(self.CHUNK)
                while data and not self.stop_audio:
                    stream.write(data)
                    data = f.readframes(self.CHUNK)

    def stop_file(self):
        self.stop_audio = True
        self.playing_audio = False

    def dialing_handler(self):
        """
        Main function of the telephone that handles the dialing if the receiver is lifted or hooked.
        :return:
        """
        number = ''
        while not self.finish:
            if not self.receiver_down:  # Handling of the dialing when the receiver is lifted
                try:
                    c = self.number_q.get(timeout=5)
                    number += str(c)
                except Queue.Empty:
                    if number is not '':
                        print("Dialing: %s" % number)
                        self.stop_file()
                        self.phone_manager.call(number)
                        number = ''
                    pass

            else:  # Handling of the dialing when the receiver is down
                if self.playing_audio:
                    self.stop_file()
                try:
                    c = self.number_q.get(timeout=5)
                    print("Selected %d" % c)
                    if c == 9:
                        print("Turning system off")
                        self.start_file("/home/pi/telefonoa/turnoff.wav")
                        time.sleep(6)
                        call("sudo shutdown -h now", shell=True)
                    elif c <= len(self.phonebook):
                        print("Shortcut action %d: Automatic dial" % c)
                        number = self.phonebook[c - 1]['number']
                        print(number)
                        time.sleep(4)
                        self.phone_manager.call(number)
                except Queue.Empty:
                    pass

    def close(self):
        self.rotary_dial.finish = True
        self.phone_manager.loop.quit()
        GPIO.cleanup()


if __name__ == '__main__':
    HOERER_PIN = 13
    NS_PIN = 19

    t = Telephone(NS_PIN, HOERER_PIN)
    try:
        t.dialing_handler()
    except KeyboardInterrupt:
        pass
    t.close()
