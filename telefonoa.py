import RPi.GPIO as GPIO
import datetime
import dbus
import dbus.mainloop.glib
from gi.repository import GLib

import time
from threading import Thread
from threading import Event
import queue as Queue
import numpy as np
import struct


class RotaryDial(Thread):
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


class ReceiverStatus():
    def __init__(self, receiver_pin, phone_manager):
        self.receiver_pin = receiver_pin
        self.phone_manager = phone_manager
        GPIO.setup(self.receiver_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        if GPIO.input(self.receiver_pin) is GPIO.HIGH:
            self.receiver_down = True
        else:
            self.receiver_down = False
        self.finish = False
        GPIO.add_event_detect(self.receiver_pin, GPIO.BOTH, callback=self.receiver_changed, bouncetime=90)

    def receiver_changed(self, pin_num):
        if self.receiver_down:
            self.receiver_down = False
        else:
            if self.phone_manager.call_in_progress:
                self.phone_manager.end_call()
            self.receiver_down = True


class PhoneManager(object):
    def __init__(self):
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
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.loop = GLib.MainLoop()
        self._thread = Thread(target=self.loop.run)
        self._thread.start()

        self.org_ofono_obj.connect_to_signal("CallAdded", self.set_call_in_progress,
                                             dbus_interface='org.ofono.VoiceCallManager')

        self.org_ofono_obj.connect_to_signal("CallRemoved", self.set_call_ended,
                                             dbus_interface='org.ofono.VoiceCallManager')

    def set_call_in_progress(self, object, properties):
        print("Call in progress!")
        self.call_in_progress = True

    def set_call_ended(self, object):
        print("Call ended!")
        self.call_in_progress = False

    def end_call(self):
        self.voice_call_manager.HangupAll()

    def call(self, number, hide_id='default'):
        try:
            self.voice_call_manager.Dial(str(number), hide_id)
        except Exception as e:
            print("Cannot place the call, check format!")


class Telephone(object):
    def __init__(self, num_pin, receiver_pin):
        GPIO.setmode(GPIO.BCM)
        self.receiver_pin = receiver_pin
        self.number_q = Queue.Queue()
        self.phone_manager = PhoneManager()
        self.rotary_dial = RotaryDial(num_pin, self.number_q)
        self.receiver_status = ReceiverStatus(receiver_pin, self.phone_manager)
        self.finish = False

        # Start all threads
        self.rotary_dial.start()

    def dialing_handler(self):
        number = ''
        while not self.finish:
            if not self.receiver_status.receiver_down:
                    try:
                        c = self.number_q.get(timeout=3)
                        number += str(c)
                        print(number)
                    except Queue.Empty:
                        if number is not '':
                            print("Dialing: %s" % number)
                            self.phone_manager.call(number)
                            number = ''
                        pass

            else:
                try:
                    c = self.number_q.get(timeout=3)
                    print("Selected %d" % c)
                except Queue.Empty:
                    pass


    def close(self):
        self.rotary_dial.finish = True
        self.receiver_status.finish = True
        self.phone_manager.loop.quit()


if __name__ == '__main__':
    HOERER_PIN = 13
    NS_PIN = 19


    t = Telephone(NS_PIN, HOERER_PIN)
    try:
        t.dialing_handler()
    except KeyboardInterrupt:
        pass
    t.close()
