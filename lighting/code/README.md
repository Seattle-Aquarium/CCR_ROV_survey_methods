The code for the lighting control subsystem.

This is currently a work in progress.

The code is written in micropython and designed to run on a raspberry pi pico 2
microcontroller.


'''
mpremote mount .
Local directory . is mounted at /remote
Connected to MicroPython at /dev/cu.usbmodem1101
Use Ctrl-] or Ctrl-x to exit this shell
>
MicroPython v1.27.0 on 2025-12-09; Raspberry Pi Pico2 with RP2350
Type "help()" for more information.
>>> import main
>>> c = main.main()
Lutris Lighting System Starting...
----------------------------------------
UART0 initialized: TX=GP0, RX=GP1
Initialized 4 SeaLite lights (addresses [1, 2, 3, 4])
I2C devices found: []
Warning: INA238 not found: INA238 not found at address 0x40
----------------------------------------
System ready. Commands:
  c.set_light_level(0, 50)  # Light 0 to 50%
  c.get_light_level(0)      # Read level
  c.get_light_temperature(0)
  c.set_all_lights(50)
  c.all_off()
  c.read_power()
  c.status()
>>> 

'''