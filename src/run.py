import argparse
import glob
import os
import queue
import readline
import shutil
import signal
import tempfile
import threading
import time
from datetime import datetime

import cv2 as cv
import numpy as np
import serial


# Command i/o settings
STDIN_PROMPT = '> '

SERIAL_BAUDRATE = 19200

_CMD_QUEUE_SENTINEL = object()


# Video settings
VID_DEVICE_ID = 2
VID_WIDTH, VID_HEIGHT, VID_FPS = 1280, 720, 20
VID_FOURCC = cv.VideoWriter_fourcc(*'MJPG')
VID_OUT_FOURCC = cv.VideoWriter_fourcc(*'XVID')

VID_OUT_BORDER_L, VID_OUT_BORDER_T, VID_OUT_BORDER_R, VID_OUT_BORDER_B = 182, 60, 1092, 660
VID_OUT_WIDTH, VID_OUT_HEIGHT = VID_OUT_BORDER_R - VID_OUT_BORDER_L, VID_OUT_BORDER_B - VID_OUT_BORDER_T
VID_OUT_POSTRUN, VID_OUT_POSTRUN_FAULT = 3., 10.

RESULT_TEMPLATE_DIGITS = [str(num) for num in range(10)]
RESULT_TEMPLATE_TYPES = ['practice', 'try-again', 'f', 'm'] + RESULT_TEMPLATE_DIGITS
RESULT_TEMPLATE_HEIGHT = 23
RESULT_BORDER_EXT_L, RESULT_BORDER_EXT_T, RESULT_BORDER_EXT_R, RESULT_BORDER_EXT_B = 430, 575, 680, 598
RESULT_BORDER_L, RESULT_BORDER_T, RESULT_BORDER_R, RESULT_BORDER_B = 670, 575, VID_OUT_BORDER_R, 598
RESULT_THRESHOLD = 0.95

NUM_RECORD_CMDS = 2
RECORD_START, RECORD_KILL = range(NUM_RECORD_CMDS)

NUM_VID_STATES = 4
VID_ST_UNKNOWN, VID_ST_READY_RUN, VID_ST_FINISHED, VID_ST_TRY_AGAIN = range(NUM_VID_STATES)

_REPLAYS_DIRNAME = 'replays'
_RUNLOG_FILENAME = 'runlog.csv'


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


def process_video(comm, runlog_filename, replays_dirname):
    """ Video processing thread """
    # Result templates
    templates = {t: cv.imread(f'templates/{t}.jpg') for t in RESULT_TEMPLATE_TYPES}

    # Open video
    cap = cv.VideoCapture(VID_DEVICE_ID)
    cap.set(cv.CAP_PROP_FOURCC, VID_FOURCC)
    cap.set(cv.CAP_PROP_FRAME_WIDTH, VID_WIDTH)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, VID_HEIGHT)
    cap.set(cv.CAP_PROP_FPS, VID_FPS)

    postrun_end_time, curr_result = None, None
    out_video, out_video_tmp_filename = None, None
    while True:
        # Get next frame
        ret, frame = cap.read()
        if not ret:
            continue
        frame = frame[VID_OUT_BORDER_T:VID_OUT_BORDER_B, VID_OUT_BORDER_L:VID_OUT_BORDER_R]

        result_ext_frame = frame[RESULT_BORDER_EXT_T:RESULT_BORDER_EXT_B, RESULT_BORDER_EXT_L:RESULT_BORDER_EXT_R]
        result_frame = frame[RESULT_BORDER_T:RESULT_BORDER_B, RESULT_BORDER_L:RESULT_BORDER_R]

        # Get current state
        res_try_again = np.max(cv.matchTemplate(result_ext_frame, templates['try-again'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD
        res_practice = np.max(cv.matchTemplate(result_ext_frame, templates['practice'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD
        res_f = np.max(cv.matchTemplate(result_frame, templates['f'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD
        res_m = np.max(cv.matchTemplate(result_frame, templates['m'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD

        if res_try_again:
            state = VID_ST_TRY_AGAIN
        elif res_f or res_m:
            state = VID_ST_FINISHED
        elif res_practice:
            state = VID_ST_READY_RUN
        else:
            state = VID_ST_UNKNOWN

        with comm:
            # Notify threads waiting for the state change
            if state != comm.state:
                comm.state_changed.notify()
            comm.state = state

            # Process recording command
            if comm.record_cmd == RECORD_START:
                if not comm.recording:
                    fh, out_video_tmp_filename = tempfile.mkstemp(prefix='replay-', suffix='.avi')
                    print(f'\rSaving to {out_video_tmp_filename}\n{STDIN_PROMPT}', end='')
                    os.close(fh)
                    out_video = cv.VideoWriter(out_video_tmp_filename, VID_OUT_FOURCC, VID_FPS, (VID_OUT_WIDTH, VID_OUT_HEIGHT))

                    comm.recording, postrun_end_time = True, None

            elif comm.record_cmd == RECORD_KILL:
                out_video.release()
                os.remove(out_video_tmp_filename)

                comm.run_cmd, comm.record_cmd, comm.recording = None, None, False

                print(f'\rRecording killed\n{STDIN_PROMPT}', end='')

            if comm.recording:
                # Save frame
                out_video.write(frame)

                # Check if postrun is finished
                if postrun_end_time is not None and postrun_end_time < time.time():
                    out_video.release()

                    # Find max version
                    max_version = 0
                    for f in glob.iglob(f'{replays_dirname}/{str(curr_result):0>5s}-*.avi'):
                        f = os.path.basename(f)
                        version = int(f[6:-4])
                        max_version = max(version, max_version)

                    max_version += 1
                    out_filename = f'{replays_dirname}/{str(curr_result):0>5s}-{max_version:0>2d}.avi'
                    shutil.move(out_video_tmp_filename, out_filename)

                    # Save result to log file
                    with open(runlog_filename, 'a') as f:
                        r = curr_result if curr_result == 'fault' else f'{curr_result/100:.2f}'
                        f.write(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")},"{comm.run_cmd}",{r},{out_filename}\n')

                    comm.run_cmd, comm.record_cmd, comm.recording = None, None, False

                    print(f'\rPostrun finished; {out_video_tmp_filename} moved to {out_filename}\n{STDIN_PROMPT}', end='')

                elif postrun_end_time is None:
                    # Check if the run is finished
                    if res_f:
                        postrun_end_time = time.time() + VID_OUT_POSTRUN_FAULT
                        curr_result = 'fault'

                        print(f'\rFailed throw\n{STDIN_PROMPT}', end='')

                    elif res_m:
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

                        postrun_end_time = time.time() + VID_OUT_POSTRUN
                        curr_result = result

                        print(f'\rSuccessful throw: {result/100:.2f} m.\n{STDIN_PROMPT}', end='')

        # Show preview
        cv.imshow('C64', frame)

        cv.waitKey(1)

    cap.release()


class VideoComm:
    """ Class used for communication with the video thread """
    def __init__(self):
        self._lock = threading.Lock()

        self.state = VID_ST_UNKNOWN
        # self.state_changed = threading.Condition(threading.Lock())
        self.state_changed = threading.Condition(self._lock)

        self.run_cmd, self.record_cmd, self.recording = None, None, False

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()


def prepare_for_next_run(ser, video_comm):
    """ Skip through finished and 'try again' states """
    print(f'\rPreparing for next run\n{STDIN_PROMPT}', end='')

    while True:
        with video_comm:
            if video_comm.state == VID_ST_READY_RUN:
                print(f'\rPrepared\n{STDIN_PROMPT}', end='')
                return
            # elif video_comm.state == VID_ST_POSTRUN:
            #     continue
            elif video_comm.state in [VID_ST_FINISHED, VID_ST_TRY_AGAIN]:
                ser.write('f\n'.encode())
                video_comm.state_changed.wait()
                continue
            else:
                video_comm.state_changed.wait()
                continue
                # raise RuntimeError(f'Can\'t process video state {video_comm.state}')


# Ignore SIGINT
signal.signal(signal.SIGINT, signal.SIG_IGN)


def process(serial_port, runlog_filename, replays_dirname):
    cmd_queue = queue.Queue()
    video_comm = VideoComm()

    with serial.Serial(serial_port, SERIAL_BAUDRATE) as ser:
        threading.Thread(target=read_stdin, args=(cmd_queue,), daemon=True).start()
        threading.Thread(target=read_serial, args=(ser, cmd_queue), daemon=True).start()
        threading.Thread(target=process_video, args=(video_comm, runlog_filename, replays_dirname), daemon=True).start()

        last_cmd = None
        while True:
            # Read command
            cmd_src, cmd = cmd_queue.get()
            if cmd_src == 'serial':
                # Command from serial
                print(f'\r{cmd}\n{STDIN_PROMPT}', end='')
                continue
            else:
                # Command from stdin
                # Yield control, so that `input` has time to run
                time.sleep(1e-4)

            # Don't process the next command if we are currently recording
            with video_comm:
                if video_comm.recording and cmd is not _CMD_QUEUE_SENTINEL and (
                        cmd in ['w', 's', 'a', 'd', 'f', 'l', 'p'] or cmd.startswith('r ')):
                    print(f'\nWe are currently recording; please kill the recording first\n{STDIN_PROMPT}', end='')
                    continue

            # No command; enter pressed in console
            if not cmd:
                continue

            # ctrl-d entered; finish
            if cmd is _CMD_QUEUE_SENTINEL:
                print('\rBye')
                break

            elif cmd in ['w', 's', 'a', 'd', 'f']:
                cmd += '\n'
                ser.write(cmd.encode())

            elif cmd.startswith('r '):
                prepare_for_next_run(ser, video_comm)

                with video_comm:
                    video_comm.run_cmd = cmd[2:]
                    video_comm.record_cmd = RECORD_START

                cmd += '\n'
                last_cmd = cmd.encode()
                ser.write(last_cmd)

            elif cmd == 'l':
                if last_cmd is None:
                    print(f'\rNo last command\n{STDIN_PROMPT}', end='')
                    continue

                print(f'\rRunning last command: "{last_cmd[:-1].decode()}"\n{STDIN_PROMPT}', end='')

                prepare_for_next_run(ser, video_comm)

                with video_comm:
                    video_comm.run_cmd = last_cmd.decode()[2:-1]
                    video_comm.record_cmd = RECORD_START

                ser.write(last_cmd)

            elif cmd == 'p':
                prepare_for_next_run(ser, video_comm)

            elif cmd == 'k':
                # Kill recording
                with video_comm:
                    if not video_comm.recording:
                        print(f'\rNothing to kill; we\'re not currently recording\n{STDIN_PROMPT}', end='')
                        continue

                    video_comm.record_cmd = RECORD_KILL

            elif cmd == 'q':
                print('Bye')
                break

            else:
                print(f'\rUnrecognised input: {cmd}\n{STDIN_PROMPT}', end='')


def main():
    parser = argparse.ArgumentParser(description='..')
    parser.add_argument('-p', '--port', dest='serial_port', help='Device name, e.g. \'/dev/ttyUSB0\'', required=True)
    parser.add_argument('-o', '--out-dir', dest='out_dirname', help='Directory to store runlog and replays', required=True)

    args = parser.parse_args()

    # Out dir
    runlog_filename = f'{args.out_dirname}/{_RUNLOG_FILENAME}'
    replays_dirname = f'{args.out_dirname}/{_REPLAYS_DIRNAME}'
    os.makedirs(replays_dirname, exist_ok=True)

    # Process
    process(args.serial_port, runlog_filename, replays_dirname)


if __name__ == '__main__':
    main()
