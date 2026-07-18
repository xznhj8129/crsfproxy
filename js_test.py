"""Usage:
python js_test.py
"""

import time

import pygame

def main():
    pygame.init()
    pygame.joystick.init()

    if pygame.joystick.get_count() == 0:
        print("No joystick detected.")
        return

    js = pygame.joystick.Joystick(0)
    js.init()
    print(f"Joystick '{js.get_name()}' axes={js.get_numaxes()} buttons={js.get_numbuttons()}")

    while True:
        pygame.event.pump()
        axes = [round(js.get_axis(i), 3) for i in range(js.get_numaxes())]
        buttons = [js.get_button(i) for i in range(js.get_numbuttons())]
        print("axes", axes, "buttons", buttons)
        time.sleep(0.1)

if __name__ == "__main__":
    main()
