from machine import UART, Pin
import time

uart1 = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1))
uart1.write('!001:lout=0\r\n')  # write 5 bytes

time.sleep(0.1)

uart1.write('!001:INFO?\r\n')

time.sleep(0.1)

while True:
    if uart1.any():
        data = uart1.read()
        if data:
            # Decode bytes to a string and print
            try:
                message = data.decode("utf-8")
                print("Received:", message)
            except ValueError:
                print("Received non-UTF-8 data:", data)
    time.sleep(0.1)