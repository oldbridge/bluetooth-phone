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
        self.finish = False

    def run(self):
        while not self.finish():
            pass


class Telephone:
    def __init__(self, num_pin, receiver_pin):
        self.receiver_pin = receiver_pin
        self.number_q = Queue.Queue()
        self.rotary_dial = RotaryDial(num_pin, self.number_q)
        self.receiver_status = ReceiverStatus(receiver_pin)
        self.dialer = Dialer()

        self.rotary_dial.start()
        self.receiver_status.start()
        self.dialer.start()

    def receiver_status_show(self):
        print(self.receiver_status.receiver_down)

    def dial_tone(self):
        pass

    def close(self):
        self.rotary_dial.finish = True
        self.receiver_status.finish = True
        self.dialer.finisg = True


if __name__ == '__main__':
    HOERER_PIN = 13
    NS_PIN = 19
    GPIO.setmode(GPIO.BCM)



    t = Telephone(NS_PIN, HOERER_PIN)
    for i in range(10):
        t.receiver_status_show()
        time.sleep(1)
    t.close()