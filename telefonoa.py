import RPi.GPIO as GPIO
import datetime
import time
from threading import Thread
from threading import Event
import queue as Queue
import pyaudio
import numpy as np


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
        tone_f = 440
        points = 8000
        self.duration = points / self.fs
        times = np.linspace(0, self.duration, points, endpoint=False)
        self.tone_samples = np.array((np.sin(times * tone_f * 2 * np.pi) + 1.0) * 127.5, dtype=np.int8).tostring()
        self.tone = Event()
        self.finish = False

    def run(self):
        while True:  # Keep the thread alive forever
            while not self.tone.is_set():  # Wait UNTIL the tone flag is set
                pass
            audio = pyaudio.PyAudio()
            stream = audio.open(format=audio.get_format_from_width(1),
                                     channels=1,
                                     rate=self.fs,
                                     output=True)
            while self.tone.is_set():  # Play the tone WHILE the tone flas is set
                stream.write(self.tone_samples)

            stream.close()
            audio.terminate()


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
