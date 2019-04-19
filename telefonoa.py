import RPi.GPIO as GPIO
import datetime
import time
from threading import Thread
import queue as Queue


class RotaryDial(Thread):
    def __init__(self, ns_pin, number_queue):
        Thread.__init__(self)
        self.pin = ns_pin
        self.number_q = number_queue
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self.value = 0
        self.timeout_time = 500
        self.finish = False

    def run(self):
        while not self.finish:
            c = GPIO.wait_for_edge(self.pin, GPIO.RISING, bouncetime=90, timeout=self.timeout_time)
            if c is None:
                if self.value > 0:
                    print("Detected: %d" % self.value)
                    self.number_q.put(self.value)
                    self.value = 0
                else:
                    self.value = 0
            else:
                self.value += 1


class Telephone:
    def __init__(self, num_pin, receiver_pin):
        self.receiver_pin = receiver_pin
        self.number_q = Queue.Queue()
        self.rotary_dial = RotaryDial(num_pin, self.number_q)
        self.rotary_dial.start()

    def receiver_status(self):
        if GPIO.input(self.receiver_pin) == GPIO.LOW:
            print("Up")
        else:
            print("Down")

    def close(self):
        self.rotary_dial.finish = True


if __name__ == '__main__':
    HOERER_PIN = 13
    NS_PIN = 19
    GPIO.setmode(GPIO.BCM)

    GPIO.setup(HOERER_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    t = Telephone(NS_PIN, HOERER_PIN)
    for i in range(10):
        t.receiver_status()
        time.sleep(1)
    t.close()