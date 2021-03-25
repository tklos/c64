#include <stdio.h>
#include <Joystick.h>


const int BUTTON_DELAY = 50;

#define MAX_CMD_LEN 50
char cmd_arr[MAX_CMD_LEN];
#define NUM_CMD_ELEMS 7
int cmd_elems[NUM_CMD_ELEMS];

#define BUFLEN 100
char buf[BUFLEN];


//#define USB_SERIAL



#ifdef USB_SERIAL
	Serial_ &serial = Serial;
#else
	HardwareSerial &serial = Serial1;
#endif



void setup() {
	delay(1000);

	serial.begin(19200);

	Joystick.begin(true);
}



void move_axis(char dir, int delay_=BUTTON_DELAY) {
	switch (dir) {
		case 'w':
			Joystick.setYAxis(-127); break;
		case 's':
			Joystick.setYAxis(127); break;
		case 'a':
			Joystick.setXAxis(-127); break;
		case 'd':
			Joystick.setXAxis(127); break;
		default:
			return;
	}

	delay(delay_);

	switch (dir) {
		case 'w':
		case 's':
			Joystick.setYAxis(0); break;
		case 'a':
		case 'd':
			Joystick.setXAxis(0); break;
	}
}


void press_button(int delay_=BUTTON_DELAY) {
	Joystick.pressButton(1);
	delay(delay_);
	Joystick.releaseButton(1);
}


void run(String &cmd) {
	if (cmd.length() >= MAX_CMD_LEN) {
		serial.println("Command too long");
		return;
	}

	/* Parse command */
	cmd.toCharArray(cmd_arr, MAX_CMD_LEN-1);
	char *ptr = strtok(cmd_arr, ",");
	int i;
	for (i = 0; i < NUM_CMD_ELEMS; i++) {
		if (ptr == NULL) {
			snprintf(buf, BUFLEN-1, "Expected %d parameters, got %d", NUM_CMD_ELEMS, i);
			serial.println(buf);
			return;
		}

		cmd_elems[i] = atoi(ptr);
		ptr = strtok(NULL, ",");
	}
	if (ptr != NULL) {
		snprintf(buf, BUFLEN-1, "Expected %d parameters, got more", NUM_CMD_ELEMS);
		serial.println(buf);
		return;
	}

	int f1 = cmd_elems[0], s1 = cmd_elems[1];  // fire 1, space 1
	int num_steps = cmd_elems[2], fr = cmd_elems[3], sr = cmd_elems[4];  // fire while running, space while running
	int st = cmd_elems[5], lt = cmd_elems[6];  // space before throwing, left axis while throwing

	snprintf(buf, BUFLEN-1, "Running f1=%d s1=%d num_steps=%d fr=%d sr=%d st=%d lt=%d", f1, s1, num_steps, fr, sr, st, lt);
	serial.println(buf);

	/* Press fire button */
	if (f1 >= 0)
		press_button(f1);
	if (s1 > 0)
		delay(s1);

	/* Run; press and wait, except for the last time */
	for (int i = 0; i < num_steps-1; i++) {
		press_button(fr);
		delay(sr);
	}
	press_button(fr);

	/* Throw */
	if (st > 0)
		delay(st);
	move_axis('a', lt);

	serial.println("Finished run");
}


void loop() {
	if (!serial.available())
		return;


	/* Read command */
	String cmd;
	char c;
	while (true) {
		if (!serial.available())
			continue;

		c = serial.read();
		if (c == '\n')
			break;
		cmd += c;
	}


	/* Process command */
	if (cmd == "w" || cmd == "s" || cmd == "a" || cmd == "d")
		move_axis(cmd[0]);

	else if (cmd == "f")
		press_button();

	else if (cmd[0] == 'r') {
		cmd = cmd.substring(2);
		run(cmd);
	}

	else
		serial.println("Unknown command");
}
