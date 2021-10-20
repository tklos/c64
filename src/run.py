"""Run THEC64 Summer Games II's javelin throw in continuous mode.

Usage: run.py -p SERIAL_PORT -d VIDEO_DEVICE_ID -o OUT_DIRNAME
    -p SERIAL_PORT, --port SERIAL_PORT
        Serial device name, e.g. "/dev/ttyUSB0"
    -d VIDEO_DEVICE_ID, --video-device-id VIDEO_DEVICE_ID
        Video device ID
    -o OUT_DIRNAME, --out-dir OUT_DIRNAME
        Directory to store runlog and replays

This program does the following in an infinite loop:
 - randomly choose the number of steps, the length of each step and the throwing angle
 - send those parameters to THEC64
 - capture video output from THEC64
 - wait for the attempt to finish and read the result
 - save the video to disk and the result in a csv file

Results are saved in {OUT_DIRNAME}/runlog.csv; videos in {OUT_DIRNAME}/replays/.

Runlog file format (csv):
    date: str
        YYYY-mm-dd HH:MM:SS
    cmd: str
        command used for the given run
    result: float or str
        float or "fault"
    replay-filename: str

Replay filename format: {result}-s{steps}-p{pressed}-{counter}.avi
    result: in cm or "fault"
    steps: number of steps
    pressed: button pressed time [ms]
    counter: next available integer that makes the filename unique
"""
import argparse
import glob
import os
import random
import shutil
import tempfile
import time
from datetime import datetime

import cv2 as cv
import numpy as np
import serial


SERIAL_BAUDRATE = 19200

# Video settings
VID_WIDTH, VID_HEIGHT, VID_FPS = 1280, 720, 20
VID_FOURCC = cv.VideoWriter_fourcc(*'MJPG')
VID_OUT_FOURCC = cv.VideoWriter_fourcc(*'XVID')

VID_OUT_BORDER_L, VID_OUT_BORDER_T, VID_OUT_BORDER_R, VID_OUT_BORDER_B = 182, 60, 1092, 660
VID_OUT_WIDTH, VID_OUT_HEIGHT = VID_OUT_BORDER_R - VID_OUT_BORDER_L, VID_OUT_BORDER_B - VID_OUT_BORDER_T
VID_OUT_POSTRUN, VID_OUT_POSTRUN_FAULT = 3., 10.

MAX_RECORD_TIME = 60.

NUM_RUN_STATES = 4
RUN_ST_UNKNOWN, RUN_ST_READY_RUN, RUN_ST_FINISHED, RUN_ST_TRY_AGAIN = range(NUM_RUN_STATES)

RESULT_TEMPLATE_DIGITS = [str(num) for num in range(10)]
RESULT_TEMPLATE_TYPES = ['practice', 'try-again', 'f', 'm'] + RESULT_TEMPLATE_DIGITS
RESULT_TEMPLATE_HEIGHT = 23
RESULT_BORDER_EXT_L, RESULT_BORDER_EXT_T, RESULT_BORDER_EXT_R, RESULT_BORDER_EXT_B = 430, 575, 680, 598
RESULT_BORDER_L, RESULT_BORDER_T, RESULT_BORDER_R, RESULT_BORDER_B = 670, 575, VID_OUT_BORDER_R, 598
RESULT_THRESHOLD = 0.95
TEMPLATES = {t: cv.imread(f'templates/{t}.jpg') for t in RESULT_TEMPLATE_TYPES}

_REPLAYS_DIRNAME = 'replays'
_RUNLOG_FILENAME = 'runlog.csv'


## Run parameters
# Number of milliseconds to wait before starting to run
INITIAL_MIN, INITIAL_MAX = 100, 1150
# Throwing angle: number of milliseconds to keep joystick in the left position
THROW_MIN, THROW_MAX = 300, 500
# Allowed 'pressed' values per 'steps' value; None if none are allowed
# For example PRESSED_PER_STEPS[30] = (25, 60) means that if we take 30 steps, each can be between 25 and 60 ms long
PRESSED_PER_STEPS = [
    *([None] * 10),  # 0-9
    *([None] * 10),  # 10-19
    (40, 80), (40, 80), (40, 80), (35, 80), (35, 80), (30, 80), (30, 75), (30, 70), (30, 70), (30, 65),  # 20-29
    (25, 60), (25, 58), (25, 56), (25, 54), (25, 52), (20, 50), (20, 48), (20, 46), (20, 44), (20, 42),  # 30-39
    (15, 40), (15, 39), (15, 38), (15, 37), (15, 36), (15, 35), (15, 34), (15, 33), (15, 32), (15, 31),  # 40-49
    (10, 30), (10, 29), (10, 29), (10, 28), (10, 28), (10, 27), (10, 27), (10, 26), (10, 26), (10, 25),  # 50-59
    (10, 25), (10, 24), (10, 23), (10, 23), (10, 22), (10, 22), (10, 21), (10, 21), (10, 20), (10, 20),  # 60-69
    (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20),  # 70-79
    (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20), (10, 20),  # 80-89
    (10, 20),  # 90
]


def open_video(device_id):
    """Open video device.

    Parameters:
        device_id: int

    Returns: cv2.VideoCapture
    """
    cap = cv.VideoCapture(device_id)
    cap.set(cv.CAP_PROP_FOURCC, VID_FOURCC)
    cap.set(cv.CAP_PROP_FRAME_WIDTH, VID_WIDTH)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, VID_HEIGHT)
    cap.set(cv.CAP_PROP_FPS, VID_FPS)

    return cap


def prepare_for_next_run(ser, cap):
    """Skip through finished and "try again" states.

    Normally each run follows the schema:
    "practice" -> "practice" + result ("{distance} m" or "fault") -> "try again" -> "practice"

    This function sends "fire" command until we get to the "practice" state.

    Parameters:
        ser: serial.Serial
        cap: cv2.VideoCapture
    """
    print('Preparing for next run')
    prev_state = None
    while True:
        # Get next frame
        ret, frame = cap.read()
        if not ret:
            continue
        frame = frame[VID_OUT_BORDER_T:VID_OUT_BORDER_B, VID_OUT_BORDER_L:VID_OUT_BORDER_R]

        result_ext_frame = frame[RESULT_BORDER_EXT_T:RESULT_BORDER_EXT_B, RESULT_BORDER_EXT_L:RESULT_BORDER_EXT_R]
        result_frame = frame[RESULT_BORDER_T:RESULT_BORDER_B, RESULT_BORDER_L:RESULT_BORDER_R]

        # Get current state
        res_try_again = np.max(cv.matchTemplate(result_ext_frame, TEMPLATES['try-again'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD
        res_practice = np.max(cv.matchTemplate(result_ext_frame, TEMPLATES['practice'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD
        res_f = np.max(cv.matchTemplate(result_frame, TEMPLATES['f'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD
        res_m = np.max(cv.matchTemplate(result_frame, TEMPLATES['m'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD

        if res_try_again:
            state = RUN_ST_TRY_AGAIN
        elif res_f or res_m:
            state = RUN_ST_FINISHED
        elif res_practice:
            state = RUN_ST_READY_RUN
        else:
            state = RUN_ST_UNKNOWN

        if state == RUN_ST_READY_RUN:
            print('Prepared')
            break
        elif state in [RUN_ST_FINISHED, RUN_ST_TRY_AGAIN]:
            if state != prev_state:
                # print(f'{state=}, waiting..')
                prev_state = state
                ser.write(b'f\n')
            continue
        else:
            # if state != prev_state:
            #     print('Unknown state, waiting..')
            continue


def process_throw(cap, run_cmd, num_steps, pressed, runlog_filename, replays_dirname):
    """Send the run command and save the result.

    Parameters:
        cap: cv2.VideoCapture
            Opened video device
        run_cmd: str
            Command to be executed
        num_steps: int
        pressed: int
        runlog_filename: str
        replays_dirname: str

    Runlog file format (csv):
        date: str
            YYYY-mm-dd HH:MM:SS
        cmd: str
            command used for the given run
        result: float or str
            float or 'fault'
        replay-filename: str

    Replay filename format: {result}-s{steps}-p{pressed}-{counter}.avi
        result: in cm or "fault"
        steps: number of steps
        pressed: button pressed time [ms]
        counter: next available integer that makes the filename unique
    """
    # Start video recording
    fh, out_video_tmp_filename = tempfile.mkstemp(prefix='replay-', suffix='.avi')
    print(f'Saving to {out_video_tmp_filename}')
    os.close(fh)
    out_video = cv.VideoWriter(out_video_tmp_filename, VID_OUT_FOURCC, VID_FPS, (VID_OUT_WIDTH, VID_OUT_HEIGHT))

    start_time = time.time()
    postrun_end_time, curr_result = None, None
    while True:
        # Error if recording for too long
        if time.time() - start_time > MAX_RECORD_TIME:
            raise RuntimeError('Recording for too long; sth is wrong..')

        # Get next frame
        ret, frame = cap.read()
        if not ret:
            continue
        frame = frame[VID_OUT_BORDER_T:VID_OUT_BORDER_B, VID_OUT_BORDER_L:VID_OUT_BORDER_R]

        result_frame = frame[RESULT_BORDER_T:RESULT_BORDER_B, RESULT_BORDER_L:RESULT_BORDER_R]

        # Save frame
        out_video.write(frame)
        cv.imshow('C64', frame)

        # Get current state
        res_f = np.max(cv.matchTemplate(result_frame, TEMPLATES['f'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD
        res_m = np.max(cv.matchTemplate(result_frame, TEMPLATES['m'], cv.TM_CCOEFF_NORMED)) > RESULT_THRESHOLD

        if postrun_end_time is not None and postrun_end_time < time.time():
            out_video.release()

            # Find max version
            max_version = 0
            for f in glob.iglob(f'{replays_dirname}/{str(curr_result):0>5s}-s{num_steps}-p{pressed}-*.avi'):
                f = os.path.basename(f)
                version = int(f[14:-4])
                max_version = max(version, max_version)

            max_version += 1
            out_filename = f'{replays_dirname}/{str(curr_result):0>5s}-s{num_steps}-p{pressed}-{max_version:0>2d}.avi'
            shutil.move(out_video_tmp_filename, out_filename)

            # Save result to log file
            with open(runlog_filename, 'a') as f:
                r = curr_result if curr_result == 'fault' else f'{curr_result/100:.2f}'
                f.write(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")},"{run_cmd}",{r},{out_filename}\n')

            print(f'Postrun finished; {out_video_tmp_filename} moved to {out_filename}')

            return

        elif postrun_end_time is None:
            # Check if the run is finished
            if res_f:
                postrun_end_time = time.time() + VID_OUT_POSTRUN_FAULT
                curr_result = 'fault'

                print(f'Failed throw')

            elif res_m:
                # Read the result
                res = [cv.matchTemplate(result_frame, TEMPLATES[t], cv.TM_CCOEFF_NORMED) for t in RESULT_TEMPLATE_DIGITS]
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

                print(f'Successful throw: {result/100:.2f} m.')

        cv.waitKey(1)


def process(serial_port, video_device_id, runlog_filename, replays_dirname):
    """Main processing function.

    This function, in an infinite loop, randomly chooses the number of steps, the length of each step and the throwing angle
    and processes the throw.

    This function never terminates.

    Parameters:
        serial_port: str
        video_device_id: int
        runlog_filename: str
        replays_dirname: str

    Returns: None
    """
    # Create a list of allowed steps-pressed pairs
    states = []
    for step, val in enumerate(PRESSED_PER_STEPS):
        if val is None:
            continue
        first, last = val
        for pressed in range(first, last+1):
            states.append((step, pressed))

    cap = open_video(video_device_id)
    with serial.Serial(serial_port, SERIAL_BAUDRATE) as ser:
        while True:
            print('')

            # Prepare command
            initial = random.randrange(INITIAL_MIN, INITIAL_MAX+1)
            throw = random.randrange(THROW_MIN, THROW_MAX+1)
            num_steps, pressed = random.choice(states)

            cmd = f'r -1,{initial},{num_steps},{pressed},{pressed},0,{throw}\n'.encode()

            # Send command
            print(f'Running command: {cmd[:-1].decode()}')
            prepare_for_next_run(ser, cap)
            ser.write(cmd)

            # Record throw
            process_throw(cap, cmd[2:-1].decode(), num_steps, pressed, runlog_filename, replays_dirname)

    # This is not reachable
    cap.release()


def main():
    parser = argparse.ArgumentParser(description='Run THEC64 Summer Games II\'s javelin throw in continuous mode')
    parser.add_argument('-p', '--port', dest='serial_port', help='Serial device name, e.g. "/dev/ttyUSB0"', required=True)
    parser.add_argument('-d', '--video-device-id', dest='video_device_id', help='Video device ID', type=int, required=True)
    parser.add_argument('-o', '--out-dir', dest='out_dirname', help='Directory to store runlog and replays', required=True)

    args = parser.parse_args()

    # Out dir
    runlog_filename = f'{args.out_dirname}/{_RUNLOG_FILENAME}'
    replays_dirname = f'{args.out_dirname}/{_REPLAYS_DIRNAME}'
    os.makedirs(replays_dirname, exist_ok=True)

    # Process
    process(args.serial_port, args.video_device_id, runlog_filename, replays_dirname)


if __name__ == '__main__':
    main()
