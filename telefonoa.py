import RPi.GPIO as GPIO
import datetime
import time
from threading import Thread


def count(val):
    global c
    c = c + 1
    print("Pulse:", c)


class RotaryDial():
    def __init__(self, ns_pin):
        self.pin = ns_pin
        self.value = 0
        self.timeout_time = 500

    def counter(self):
        while True:
            c = GPIO.wait_for_edge(self.pin, GPIO.RISING, bouncetime=90, timeout=self.timeout_time)
            if c is None:
                if self.value > 0:
                    print("Detected: %d" % self.value)
                    self.value = 0
                else:
                    self.value = 0
            else:
                self.value += 1


if __name__ == '__main__':
    global c
    HOERER_PIN = 13
    NS_PIN = 19
    GPIO.setmode(GPIO.BCM)

    GPIO.setup(HOERER_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(NS_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # Count dialed number
    #count_pulses()

    #GPIO.add_event_detect(NS_PIN, GPIO.RISING, callback=count, bouncetime=100)
    r = RotaryDial(NS_PIN)

    r.counter()
    while True:
        pass