import queue
import readline
import threading
import time

import serial


STDIN_PROMPT = '> '

SERIAL_PORT = '/dev/ttyUSB0'
SERIAL_BAUDRATE = 19200

queue = queue.Queue()
_QUEUE_SENTINEL = object()


def read_stdin():
    while True:
        try:
            cmd = input(STDIN_PROMPT).strip()
            queue.put(('stdin', cmd))
        except EOFError:
            queue.put(('stdin', _QUEUE_SENTINEL))
            break


def read_serial(ser):
    while True:
        cmd = ser.readline().rstrip().decode()
        queue.put(('serial', cmd))


def main():
    with serial.Serial(SERIAL_PORT, SERIAL_BAUDRATE) as ser:
        threading.Thread(target=read_stdin, daemon=True).start()
        threading.Thread(target=read_serial, args=(ser,), daemon=True).start()

        last_cmd = None
        while True:
            # Read command
            try:
                cmd = queue.get()
            except KeyboardInterrupt:
                print('\nBye')
                break

            # Yield control, so that `input` has time to run
            time.sleep(1e-4)

            # Process command
            if cmd[0] == 'serial':
                print(f'\n{cmd[1]}\n{STDIN_PROMPT}', end='')
                continue

            cmd = cmd[1]

            if cmd == _QUEUE_SENTINEL:
                print('\nBye')
                break

            elif not cmd:
                continue

            elif cmd in ['w', 's', 'a', 'd', 'f']:
                cmd += '\n'
                ser.write(cmd.encode())

            elif cmd.startswith('r '):
                cmd += '\n'
                last_cmd = cmd.encode()
                ser.write(last_cmd)

            elif cmd == 'l':
                if last_cmd is None:
                    print(f'\nNo last command\n{STDIN_PROMPT}', end='')
                    continue

                print(f'\nRunning last command: {last_cmd}\n{STDIN_PROMPT}', end='')
                ser.write(last_cmd)

            elif cmd == 'q':
                print('Bye')
                break

            else:
                print(f'\nUnrecognised input: {cmd}\n{STDIN_PROMPT}', end='')


if __name__ == '__main__':
    main()
