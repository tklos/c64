import os
import queue
import readline
import signal
import tempfile
import threading
import time

import cv2 as cv
import numpy as np
import serial


# Command i/o settings
STDIN_PROMPT = '> '

SERIAL_PORT = '/dev/ttyUSB0'
SERIAL_BAUDRATE = 19200

_CMD_QUEUE_SENTINEL = object()


# Video settings
VID_DEVICE_ID = 2
VID_WIDTH, VID_HEIGHT, VID_FPS = 1280, 720, 20
VID_FOURCC = cv.VideoWriter_fourcc(*'MJPG')
VID_OUT_FOURCC = cv.VideoWriter_fourcc(*'XVID')

VID_OUT_BORDER_L, VID_OUT_BORDER_T, VID_OUT_BORDER_R, VID_OUT_BORDER_B = 182, 60, 1092, 660
VID_OUT_WIDTH, VID_OUT_HEIGHT = VID_OUT_BORDER_R - VID_OUT_BORDER_L, VID_OUT_BORDER_B - VID_OUT_BORDER_T

NUM_RECORD_CMDS = 2
RECORD_START, RECORD_KILL = range(1, NUM_RECORD_CMDS+1)  # Record statuses start from 1


def read_stdin(queue):
    while True:
        try:
            cmd = input(STDIN_PROMPT).strip()
            queue.put(('stdin', cmd))

            # A workaround to restore console settings
            if cmd == 'q':
                break

        except EOFError:
            queue.put(('stdin', _CMD_QUEUE_SENTINEL))
            break


def read_serial(ser, queue):
    while True:
        cmd = ser.readline().rstrip().decode()
        queue.put(('serial', cmd))


def process_video(record_cmd):
    """ Save video """
    # Open video
    cap = cv.VideoCapture(VID_DEVICE_ID)
    cap.set(cv.CAP_PROP_FOURCC, VID_FOURCC)
    cap.set(cv.CAP_PROP_FRAME_WIDTH, VID_WIDTH)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, VID_HEIGHT)
    cap.set(cv.CAP_PROP_FPS, VID_FPS)

    recording, out = False, None
    while True:
        cmd = record_cmd.value

        if cmd == RECORD_START:
            if not recording:
                fh, tmp_filename = tempfile.mkstemp(prefix='replay-', suffix='.avi')
                print(f'\rSaving to {tmp_filename}\n{STDIN_PROMPT}', end='')
                os.close(fh)
                out = cv.VideoWriter(tmp_filename, VID_OUT_FOURCC, VID_FPS, (VID_OUT_WIDTH, VID_OUT_HEIGHT))

                recording = True

        elif cmd == RECORD_KILL:
            out.release()

            record_cmd.value = 0
            recording, out = False, None

            print(f'\rRecording killed\n{STDIN_PROMPT}', end='')

        # Get next frame
        ret, frame = cap.read()
        if not ret:
            continue
        frame = frame[VID_OUT_BORDER_T:VID_OUT_BORDER_B, VID_OUT_BORDER_L:VID_OUT_BORDER_R]

        # Save frame
        if recording:
            out.write(frame)

        # Show preview
        cv.imshow('preview', frame)

        cv.waitKey(1)

    cap.release()


class SafeValue:
    """ A threading.Event-like class with an integer flag """
    def __init__(self):
        self._lock = threading.Lock()
        self._value = 0

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        with self._lock:
            self._value = value


# Ignore SIGINT
signal.signal(signal.SIGINT, signal.SIG_IGN)


def main():
    cmd_queue = queue.Queue()
    record_cmd = SafeValue()

    with serial.Serial(SERIAL_PORT, SERIAL_BAUDRATE) as ser:
        threading.Thread(target=read_stdin, args=(cmd_queue,), daemon=True).start()
        threading.Thread(target=read_serial, args=(ser, cmd_queue), daemon=True).start()
        threading.Thread(target=process_video, args=(record_cmd,), daemon=True).start()

        last_cmd = None
        while True:
            # Read command
            cmd = cmd_queue.get()

            # Yield control, so that `input` has time to run

            time.sleep(1e-4)

            ## Command from serial
            if cmd[0] == 'serial':
                print(f'\r{cmd[1]}\n{STDIN_PROMPT}', end='')
                continue

            ## Command from stdin
            cmd = cmd[1]

            # Don't process the next command if we are currently recording
            if record_cmd.value == RECORD_START and cmd is not _CMD_QUEUE_SENTINEL and (
                    cmd in ['w', 's', 'a', 'd', 'f', 'l'] or cmd.startswith('r ')):
                print(f'\nWe are currently recording; please kill the recording first\n{STDIN_PROMPT}', end='')
                continue

            if not cmd:
                continue

            if cmd is _CMD_QUEUE_SENTINEL:
                print('\rBye')
                break

            elif cmd in ['w', 's', 'a', 'd', 'f']:
                cmd += '\n'
                ser.write(cmd.encode())

            elif cmd.startswith('r '):
                record_cmd.value = RECORD_START

                cmd += '\n'
                last_cmd = cmd.encode()
                ser.write(last_cmd)

            elif cmd == 'l':
                if last_cmd is None:
                    print(f'\rNo last command\n{STDIN_PROMPT}', end='')
                    continue

                print(f'\rRunning last command: "{last_cmd[:-1].decode()}"\n{STDIN_PROMPT}', end='')
                if last_cmd.startswith(b'r '):
                    record_cmd.value = RECORD_START
                ser.write(last_cmd)

            elif cmd == 'k':
                # Kill recording
                if record_cmd.value != RECORD_START:
                    print(f'\rNothing to kill; we\'re not currently recording\n{STDIN_PROMPT}', end='')
                    continue

                record_cmd.value = RECORD_KILL

            elif cmd == 'q':
                print('Bye')
                break

            else:
                print(f'\rUnrecognised input: {cmd}\n{STDIN_PROMPT}', end='')


if __name__ == '__main__':
    main()
