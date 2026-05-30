/* ============================================================================
   Triple D - car-side firmware (Arduino UNO R3, on the ELEGOO Smart Car)
   ----------------------------------------------------------------------------
   ROLE: This board is a "dumb peripheral". It does NOT make decisions.
   It (1) samples the microphone and extracts simple acoustic features,
   (2) reads the ultrasonic range, (3) streams those up over USB serial,
   and (4) executes one-word ACTION commands sent back down by the Uno Q.

   All intelligence (ML, fusion, the DECIDE logic, human authorization)
   lives in Python on the Uno Q. Keep this sketch THIN on purpose.

   ----------------------------------------------------------------------------
   SERIAL PROTOCOL  (115200 baud, newline-terminated ASCII)
     UP   (car -> Uno Q), every TELEMETRY_MS:
            TEL,AMP:<int>,PITCH:<int>,DIST:<int>,LINE:<0|1>
     DOWN (Uno Q -> car), one per line:
            CMD,<ACTION>
          where ACTION is one of:
            IDLE | DISTRACT_ON | DISTRACT_OFF | DAZZLE_ON | DAZZLE_OFF |
            DRIVE_F | DRIVE_B | DRIVE_L | DRIVE_R | DRIVE_S | ALL_OFF
   ----------------------------------------------------------------------------
   PIN MAP --- ADJUST THESE TO MATCH YOUR WIRING / ELEGOO V4 SHIELD.
   The motor pins below assume a generic L298N. If you use the ELEGOO V4
   shield + its library, delete the drive*() bodies and call ELEGOO's
   motor functions instead. Everything else is standard.
   ============================================================================ */

// ---- Microphone (sound-sensor module analog out, e.g. KY-038/KY-037) -------
const uint8_t  MIC_PIN      = A0;
const uint16_t MIC_SAMPLES  = 200;   // samples per analysis window
const int      MIC_MID      = 512;   // DC bias of the mic signal (calibrate)

// ---- Ultrasonic (HC-SR04 on the servo mast) --------------------------------
const uint8_t  TRIG_PIN     = 12;
const uint8_t  ECHO_PIN     = 13;

// ---- Line sensor (one channel, optional) -----------------------------------
const uint8_t  LINE_PIN     = 2;

// ---- Effects (each via a transistor or the relay) --------------------------
const uint8_t  DECOY_LED_PIN = 7;    // visible/near-IR decoy beacon  (DISTRACT)
const uint8_t  HEATER_PIN    = 8;    // power resistor via relay      (DISTRACT)
const uint8_t  IR_DAZZLE_PIN = 9;    // IR LED array                  (DAZZLE)

// ---- Motors (generic L298N example - REPLACE to match your hardware) -------
const uint8_t  ENA = 5, IN1 = 3, IN2 = 4;     // left  motor(s)
const uint8_t  ENB = 6, IN3 = 10, IN4 = 11;   // right motor(s)
const uint8_t  DRIVE_SPEED = 160;             // 0-255 PWM

// ---- Timing ----------------------------------------------------------------
const unsigned long TELEMETRY_MS = 100;       // ~10 telemetry frames / second
unsigned long lastTelemetry = 0;

// ---- Incoming command buffer -----------------------------------------------
char cmdBuf[24];
uint8_t cmdLen = 0;

void setup() {
  Serial.begin(115200);
  pinMode(LINE_PIN, INPUT);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

  pinMode(DECOY_LED_PIN, OUTPUT);
  pinMode(HEATER_PIN,    OUTPUT);
  pinMode(IR_DAZZLE_PIN, OUTPUT);

  pinMode(ENA, OUTPUT); pinMode(IN1, OUTPUT); pinMode(IN2, OUTPUT);
  pinMode(ENB, OUTPUT); pinMode(IN3, OUTPUT); pinMode(IN4, OUTPUT);

  allOff();
}

void loop() {
  handleSerial();                 // non-blocking: parse any incoming command

  unsigned long now = millis();
  if (now - lastTelemetry >= TELEMETRY_MS) {
    lastTelemetry = now;
    sendTelemetry();
  }
}

/* ------------------------------------------------------------------ TELEMETRY */
void sendTelemetry() {
  int amp, pitch;
  analyzeMic(amp, pitch);
  int dist  = readDistanceCm();
  int line  = digitalRead(LINE_PIN);

  Serial.print(F("TEL,AMP:"));   Serial.print(amp);
  Serial.print(F(",PITCH:"));    Serial.print(pitch);
  Serial.print(F(",DIST:"));     Serial.print(dist);
  Serial.print(F(",LINE:"));     Serial.print(line);
  Serial.print('\n');
}

/* Acoustic feature extraction ON THE ARDUINO (as requested).
   We measure:
     amp   = peak-to-peak amplitude of the window  -> "how loud"
     pitch = dominant frequency estimate (Hz) via zero-crossing rate
             -> "how high-pitched"  (drones whine high)
   NOTE: analogRead caps the usable sample rate (~a few kHz), so by Nyquist
   this estimates pitch up to ~a couple kHz. That is enough to separate a
   high prop-whine from low ambient noise. For a sharper spectrum, swap this
   for the ArduinoFFT library or a Goertzel filter on the target band. */
void analyzeMic(int &amp, int &pitch) {
  unsigned long t0 = micros();
  int vmin = 1023, vmax = 0, crossings = 0;
  int prev = analogRead(MIC_PIN);
  for (uint16_t i = 0; i < MIC_SAMPLES; i++) {
    int v = analogRead(MIC_PIN);
    if (v < vmin) vmin = v;
    if (v > vmax) vmax = v;
    if ((prev < MIC_MID && v >= MIC_MID) || (prev >= MIC_MID && v < MIC_MID)) crossings++;
    prev = v;
  }
  unsigned long dt = micros() - t0;          // window duration in microseconds
  amp = vmax - vmin;
  float seconds = dt / 1000000.0;
  pitch = (seconds > 0.0) ? (int)((crossings / 2.0) / seconds) : 0;
}

int readDistanceCm() {
  digitalWrite(TRIG_PIN, LOW);  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  unsigned long us = pulseIn(ECHO_PIN, HIGH, 25000UL);  // 25ms timeout (~4m)
  if (us == 0) return -1;                                // out of range
  return (int)(us / 58);                                 // us -> cm
}

/* ------------------------------------------------------------------ COMMANDS */
void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      cmdBuf[cmdLen] = '\0';
      if (cmdLen > 0) parseCommand(cmdBuf);
      cmdLen = 0;
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    }
  }
}

void parseCommand(const char *line) {
  // Expect "CMD,<ACTION>"
  const char *comma = strchr(line, ',');
  const char *action = comma ? comma + 1 : line;

  if      (!strcmp(action, "DISTRACT_ON"))  { digitalWrite(DECOY_LED_PIN, HIGH); digitalWrite(HEATER_PIN, HIGH); }
  else if (!strcmp(action, "DISTRACT_OFF")) { digitalWrite(DECOY_LED_PIN, LOW);  digitalWrite(HEATER_PIN, LOW);  }
  else if (!strcmp(action, "DAZZLE_ON"))    { digitalWrite(IR_DAZZLE_PIN, HIGH); }
  else if (!strcmp(action, "DAZZLE_OFF"))   { digitalWrite(IR_DAZZLE_PIN, LOW);  }
  else if (!strcmp(action, "DRIVE_F"))      driveForward();
  else if (!strcmp(action, "DRIVE_B"))      driveBackward();
  else if (!strcmp(action, "DRIVE_L"))      driveLeft();
  else if (!strcmp(action, "DRIVE_R"))      driveRight();
  else if (!strcmp(action, "DRIVE_S"))      driveStop();
  else if (!strcmp(action, "ALL_OFF"))      allOff();
  // IDLE / unknown -> ignore
}

/* ------------------------------------------------------------------- ACTUATE */
void allOff() {
  driveStop();
  digitalWrite(DECOY_LED_PIN, LOW);
  digitalWrite(HEATER_PIN,    LOW);
  digitalWrite(IR_DAZZLE_PIN, LOW);
}

// --- Motor helpers (generic L298N; replace with ELEGOO lib if you prefer) ---
void driveForward()  { motors(true,  true);  }
void driveBackward() { motors(false, false); }
void driveLeft()     { motors(false, true);  }
void driveRight()    { motors(true,  false); }
void driveStop()     { analogWrite(ENA, 0); analogWrite(ENB, 0); }

void motors(bool leftFwd, bool rightFwd) {
  digitalWrite(IN1, leftFwd ? HIGH : LOW);
  digitalWrite(IN2, leftFwd ? LOW  : HIGH);
  digitalWrite(IN3, rightFwd ? HIGH : LOW);
  digitalWrite(IN4, rightFwd ? LOW  : HIGH);
  analogWrite(ENA, DRIVE_SPEED);
  analogWrite(ENB, DRIVE_SPEED);
}
