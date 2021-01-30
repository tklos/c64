import os
import queue
import readline
import shutil
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
VID_OUT_POSTRUN = 3.

RESULT_TEMPLATE_DIGITS = [str(num) for num in range(10)]
RESULT_TEMPLATE_TYPES = ['f', 'm'] + RESULT_TEMPLATE_DIGITS
RESULT_TEMPLATE_HEIGHT = 23
RESULT_BORDER_L, RESULT_BORDER_T, RESULT_BORDER_R, RESULT_BORDER_B = 670, 575, VID_OUT_BORDER_R, 598
RESULT_THRESHOLD = 0.95

NUM_RECORD_CMDS = 2
RECORD_START, RECORD_KILL = range(1, NUM_RECORD_CMDS+1)  # Record statuses start from 1

RECORD_DIR = 'replays'


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
    # Result templates
    templates = {t: cv.imread(f'templates/{t}.jpg') for t in RESULT_TEMPLATE_TYPES}

    # Open video
    cap = cv.VideoCapture(VID_DEVICE_ID)
    cap.set(cv.CAP_PROP_FOURCC, VID_FOURCC)
    cap.set(cv.CAP_PROP_FRAME_WIDTH, VID_WIDTH)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, VID_HEIGHT)
    cap.set(cv.CAP_PROP_FPS, VID_FPS)

    recording, out_tmp_filename, out = False, None, None
    postrun_end, curr_result = None, None
    while True:
        cmd = record_cmd.value

        if cmd == RECORD_START:
            if not recording:
                fh, out_tmp_filename = tempfile.mkstemp(prefix='replay-', suffix='.avi')
                print(f'\rSaving to {out_tmp_filename}\n{STDIN_PROMPT}', end='')
                os.close(fh)
                out = cv.VideoWriter(out_tmp_filename, VID_OUT_FOURCC, VID_FPS, (VID_OUT_WIDTH, VID_OUT_HEIGHT))

                recording, postrun_end = True, None

        elif cmd == RECORD_KILL:
            out.release()
            os.remove(out_tmp_filename)

            record_cmd.value = 0
            recording = False

            print(f'\rRecording killed\n{STDIN_PROMPT}', end='')

        # Get next frame
        ret, frame = cap.read()
        if not ret:
            continue
        frame = frame[VID_OUT_BORDER_T:VID_OUT_BORDER_B, VID_OUT_BORDER_L:VID_OUT_BORDER_R]

        if recording:
            # Save frame
            out.write(frame)

            # Check if postrun is finished
            if postrun_end is not None and postrun_end < time.time():
                out.release()

                out_filename = f'{RECORD_DIR}/{str(curr_result):0>5s}.avi'
                shutil.move(out_tmp_filename, out_filename)

                record_cmd.value = 0
                recording = False

                print(f'\rPostrun finished; {out_tmp_filename} moved to {out_filename}\n{STDIN_PROMPT}', end='')

            elif postrun_end is None:
                # Check if the run is finished
                result_frame = frame[RESULT_BORDER_T:RESULT_BORDER_B, RESULT_BORDER_L:RESULT_BORDER_R]

                res_f = cv.matchTemplate(result_frame, templates['f'], cv.TM_CCOEFF_NORMED)
                res_m = cv.matchTemplate(result_frame, templates['m'], cv.TM_CCOEFF_NORMED)

                if np.max(res_f) > RESULT_THRESHOLD:
                    postrun_end = time.time() + VID_OUT_POSTRUN
                    curr_result = 'fault'

                    print(f'\rFailed throw\n{STDIN_PROMPT}', end='')

                elif np.max(res_m) > RESULT_THRESHOLD:
                    # Read the result
                    res = [cv.matchTemplate(result_frame, templates[t], cv.TM_CCOEFF_NORMED) for t in RESULT_TEMPLATE_DIGITS]
                    digits_pos_d = {}
                    for digit, digit_res in enumerate(res):
                        loc = np.where(digit_res >= RESULT_THRESHOLD)
                        for x_pos in loc[1]:
                            digits_pos_d[x_pos] = digit

                    result = 0
                    for _, digit in sorted(digits_pos_d.items()):
                        result = result * 10 + digit

                    postrun_end = time.time() + VID_OUT_POSTRUN
                    curr_result = result

                    print(f'\rSuccessful throw: {result/100:.2f} m.\n{STDIN_PROMPT}', end='')

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
