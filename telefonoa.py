import RPi.GPIO as GPIO
import datetime
import time
from threading import Thread
from threading import Event
import queue as Queue
import alsaaudio
import numpy as np
import struct


class RotaryDial(Thread):
    def __init__(self, ns_pin, number_queue):
        Thread.__init__(self)
        self.pin = ns_pin
        self.number_q = number_queue
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self.value = 0
        self.timeout_time = 500
        self.finish = False #Event()

    def run(self):
        while not self.finish:
            #print("waiting")
            c = GPIO.wait_for_edge(self.pin, GPIO.RISING, bouncetime=90, timeout=self.timeout_time)
            #print("peak")
            if c is None:
                if self.value > 0:
                    print("Detected: %d" % self.value)
                    self.number_q.put(self.value)
                    self.value = 0
                else:
                    self.value = 0
            else:
                self.value += 1
            #print("Exit")


class ReceiverStatus(Thread):
    def __init__(self, receiver_pin):
        Thread.__init__(self)
        self.receiver_pin = receiver_pin
        GPIO.setup(self.receiver_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self.receiver_down = False
        self.finish = False

    def run(self):
        while not self.finish:
            if GPIO.input(self.receiver_pin) == GPIO.LOW:
                self.receiver_down = False
            else:
                self.receiver_down = True


class Dialer(Thread):
    def __init__(self):
        Thread.__init__(self)

        self.fs = 8000
        self.channels = 1
        self.framesize = 2
        tone_f = 425  # European dial tone frequency

        tone_f = float(self.fs) / int(self.fs / tone_f)  # Careful with the frequency, sampling_rate / f must be an integer (avoid cutting)

        self.tone = Event()
        self.finish = False

        self.setDaemon(True)
        self.device = alsaaudio.PCM()
        self.device.setchannels(1)
        self.device.setformat(alsaaudio.PCM_FORMAT_S16_LE)
        self.device.setrate(self.fs)

        # the buffersize we approximately want
        target_size = int(self.fs * self.channels * 0.125)

        # the length of a full sine wave at the frequency
        cycle_size = self.fs / tone_f
        print(cycle_size)
        # number of full cycles we can fit into target_size
        factor = int(target_size / cycle_size)

        size = max(int(cycle_size * factor), 1)

        sine = [int(32767 * np.sin(2 * np.pi * tone_f * i / self.fs)) for i in range(size)]

        self.tone_buffer = struct.pack('%dh' % size, *sine)
        #self.device.setperiodsize(int(len(self.tone_buffer) / self.framesize))

    def run(self):
        while not self.finish:  # Keep the thread alive forever
            while self.tone.is_set():  # Play the tone WHILE the tone flas is set
                self.device.write(self.tone_buffer)


class Telephone(object):
    def __init__(self, num_pin, receiver_pin):
        self.receiver_pin = receiver_pin
        self.number_q = Queue.Queue()
        self.rotary_dial = RotaryDial(num_pin, self.number_q)
        self.receiver_status = ReceiverStatus(receiver_pin)
        self.dialer = Dialer()
        self.finish = False

        # Start all threads
        self.rotary_dial.start()
        self.receiver_status.start()
        self.dialer.start()

    def wait_tone(self):
        while not self.finish:
            if not self.receiver_status.receiver_down:
                    self.dialer.tone.set()
            else:
                    self.dialer.tone.clear()


    def close(self):
        self.rotary_dial.finish = True
        self.receiver_status.finish = True
        self.dialer.finish = True


if __name__ == '__main__':
    HOERER_PIN = 13
    NS_PIN = 19
    GPIO.setmode(GPIO.BCM)

    t = Telephone(NS_PIN, HOERER_PIN)
    try:
        t.wait_tone()
    except KeyboardInterrupt:
        pass
    t.close()
