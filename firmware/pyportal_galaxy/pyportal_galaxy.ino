/*
  PyPortal galaxy: tear-free panning via ILI9341 hardware scroll.

  Scrolls a strip cut from a large astronomical mosaic across the screen, so the display slowly traverses
  a galaxy, a nebula, or a field of them.

  Instead of repainting all 76,800 pixels per frame, this draws the first
  screenful once, then each frame only bumps the panel's vertical-scroll
  register and writes the few newly exposed columns (~1 KB) into the
  panel's own frame memory.

  The ILI9341's scroll axis is its long (320 px) axis, which in the
  PyPortal's landscape mounting is exactly the horizontal pan direction,
  and the scroll wraps around -- the panel's frame memory acts as the
  ring buffer.

  The strip is never held in memory: it is read one 480-byte column at a
  time straight off the SD card, so RAM use is constant regardless of
  file size.

  Touching the left half of the screen cycles brightness; the right half
  cycles pan speed.

  Requires galaxy.dat in the SD card root: raw big-endian RGB565 pixels,
  column-major (each column is one contiguous 480-byte record), produced
  by generator/generate_strip.py.

  Board: Adafruit PyPortal M4 (Adafruit SAMD Boards package).
  Libraries: Adafruit ILI9341, Adafruit GFX, SdFat - Adafruit Fork.
*/

#include <Adafruit_ILI9341.h>
#include <SdFat.h>

// PyPortal pin numbers (Adafruit SAMD core, pyportal_m4 variant).
#define PIN_TFT_D0 34
#define PIN_TFT_WR 26
#define PIN_TFT_RS 10  // data/command select
#define PIN_TFT_CS 11
#define PIN_TFT_RST 24
#define PIN_TFT_RD 9
#define PIN_TFT_TE 12
#define PIN_BACKLIGHT 25
#define PIN_SD_CS 32
#define PIN_ESP_CS 8
#define PIN_TOUCH_YD A4
#define PIN_TOUCH_XL A5
#define PIN_TOUCH_YU A6
#define PIN_TOUCH_XR A7

// Strip image data, read from the SD card.
#define DATA_PATH "/galaxy.dat"

// Pan speed in pixels per second and loop rate. The loop runs at
// TARGET_FPS for smooth fades/touch; the accumulator advances the
// scroll by pan_speed/TARGET_FPS pixels per frame (fractional).
const float SPEED_LEVELS[] = {10, 30, 60, 120, 200};
const int NUM_SPEEDS = sizeof(SPEED_LEVELS) / sizeof(SPEED_LEVELS[0]);
const float TARGET_FPS = 50;

// Backlight fade durations at the start and end of each pass, and the
// pause on black in between passes.
const float FADE_IN_S = 0.5;
const float FADE_OUT_S = 0.5;
const float HOLD_BLACK_S = 1.0;

// Steady-state backlight levels (0.0-1.0); each screen touch cycles to the
// next, starting on the first. Fades ramp between black and the current level.
const float BRIGHTNESS_LEVELS[] = {0.1, 0.20, 0.75, 1.0};
const int NUM_BRIGHTNESS = sizeof(BRIGHTNESS_LEVELS) / sizeof(BRIGHTNESS_LEVELS[0]);

// Scroll register direction per MADCTL line order: landscape (0xA8)
// decrements. If it pans with garbage, negate.
const int SCROLL_DIR = -1;

// Screen width and height.
const int W = 320;
const int H = 240;

// Number of bytes per column.
const uint32_t COL_BYTES = H * 2;

// Backlight PWM: 120 MHz GCLK0 / 4800 = 25 kHz, ~12 bits of duty range.
const uint32_t BL_PERIOD = 4800;

// Stock PyPortal init sequence (from the CircuitPython board definition).
// Each entry: command, data length, delay in ms, then the data bytes.
const uint8_t INIT[] = {
  0xEF, 3, 0, 0x03, 0x80, 0x02,
  0xCF, 3, 0, 0x00, 0xC1, 0x30,
  0xED, 4, 0, 0x64, 0x03, 0x12, 0x81,
  0xE8, 3, 0, 0x85, 0x00, 0x78,
  0xCB, 5, 0, 0x39, 0x2C, 0x00, 0x34, 0x02,
  0xF7, 1, 0, 0x20,
  0xEA, 2, 0, 0x00, 0x00,
  0xC0, 1, 0, 0x23,              // Power control VRH
  0xC1, 1, 0, 0x10,              // Power control SAP/BT
  0xC5, 2, 0, 0x3E, 0x28,        // VCM control
  0xC7, 1, 0, 0x86,              // VCM control 2
  // MADCTL: BGR color order, landscape orientation.
  0x36, 1, 0, 0xA8,
  0x37, 2, 0, 0x00, 0x00,        // Scroll start = 0
  0x3A, 1, 0, 0x55,              // 16 bits per pixel
  0xB1, 2, 0, 0x00, 0x18,        // Frame rate control
  // Widen the vertical porches so the blanking window after each TE
  // pulse is long enough (~2.5 ms) to bump the scroll register and
  // write a full column before the panel starts scanning again.
  0xB5, 4, 0, 0x10, 0x30, 0x0A, 0x14,  // VFP=16, VBP=48 lines
  0x35, 1, 0, 0x00,              // Tearing-effect line on (V-blank pulses only)
  0xB6, 3, 0, 0x08, 0xA2, 0x27,  // Display function control
  0xF2, 1, 0, 0x00,              // 3Gamma off
  0x26, 1, 0, 0x01,              // Gamma curve
  0xE0, 15, 0, 0x0F, 0x31, 0x2B, 0x0C, 0x0E, 0x08, 0x4E, 0xF1,
               0x37, 0x07, 0x10, 0x03, 0x0E, 0x09, 0x00,
  0xE1, 15, 0, 0x00, 0x0E, 0x14, 0x03, 0x11, 0x07, 0x31, 0xC1,
               0x48, 0x08, 0x0F, 0x0C, 0x31, 0x36, 0x0F,
  0x33, 6, 0, 0x00, 0x00, 0x01, 0x40, 0x00, 0x00,  // Scroll area = full 320 lines
  0x11, 0, 120,                  // Exit sleep
  0x29, 0, 120,                  // Display on
};

Adafruit_ILI9341 tft(tft8bitbus, PIN_TFT_D0, PIN_TFT_WR, PIN_TFT_RS,
                     PIN_TFT_CS, PIN_TFT_RST, PIN_TFT_RD);
SdFat sd;
File32 data;

// One column of pixels, reused for every read.
uint16_t colbuf[H];

uint32_t total_cols;
uint32_t max_pos;
int scroll = 0;  // current VSCRSAD register value

// Brightness selection state.
int brightness_idx = 0;
float brightness = BRIGHTNESS_LEVELS[0];

// Speed selection state.
int speed_idx = 0;
float pan_speed = SPEED_LEVELS[0];
float sub_step = pan_speed / TARGET_FPS;

// Touch detection state.
bool touch_was_pressed = false;
uint32_t touch_last_cycle = 0;

// State for the periodic fps report.
uint32_t frames = 0;
uint32_t last_report = 0;

const uint32_t FRAME_PERIOD_US = 1000000 / TARGET_FPS;

int wrap(int v) {
  return ((v % W) + W) % W;
}

void fatal(const char *msg) {
  Serial.println(msg);
  while (true) delay(1000);
}

void sendInit() {
  const uint8_t *p = INIT;
  const uint8_t *end = INIT + sizeof(INIT);
  while (p < end) {
    uint8_t cmd = *p++;
    uint8_t len = *p++;
    uint8_t delay_ms = *p++;
    tft.sendCommand(cmd, p, len);
    p += len;
    if (delay_ms) delay(delay_ms);
  }
}

void setScroll() {
  tft.scrollTo(scroll);
}

// Read one strip column from disk into the column buffer.
void loadColumn(uint32_t world_col) {
  data.seekSet(world_col * COL_BYTES);
  data.read(colbuf, COL_BYTES);
}

// Write the column buffer into panel memory so that it appears at
// screen_x under the current scroll value.
void blitColumn(int screen_x) {
  int xw = wrap(screen_x + SCROLL_DIR * scroll);
  tft.startWrite();
  tft.setAddrWindow(xw, 0, 1, H);
  tft.writePixels(colbuf, H, true, true);  // file is already big-endian
  tft.endWrite();
}

// The tearing-effect pin pulses high during vertical blanking (~66 Hz);
// scroll bumps and column writes happen inside that scan-free window.
// Block until the next rising edge.
void waitForBlanking() {
  while (digitalRead(PIN_TFT_TE)) {}
  while (!digitalRead(PIN_TFT_TE)) {}
}

void backlightBegin() {
  // Let the core do the pin mux and TCC4 clock setup, then retime TCC4
  // from its default 8-bit/1.8 kHz to BL_PERIOD at full clock.
  analogWrite(PIN_BACKLIGHT, 1);
  TCC4->CTRLA.bit.ENABLE = 0;
  while (TCC4->SYNCBUSY.bit.ENABLE) {}
  TCC4->CTRLA.reg = TCC_CTRLA_PRESCALER_DIV1 | TCC_CTRLA_PRESCSYNC_GCLK;
  TCC4->PER.reg = BL_PERIOD - 1;
  while (TCC4->SYNCBUSY.bit.PER) {}
  TCC4->CC[1].reg = 0;
  while (TCC4->SYNCBUSY.bit.CC1) {}
  TCC4->CTRLA.bit.ENABLE = 1;
  while (TCC4->SYNCBUSY.bit.ENABLE) {}
}

void setBacklight(float level) {
  TCC4->CCBUF[1].reg = (uint32_t)(BL_PERIOD * level);
}

// Touch sensing: hold the resistive panel's X plate low and pull the Y
// sense line up; a press shorts the plates and pulls the sense line low.
void touchDetectMode() {
  pinMode(PIN_TOUCH_YD, INPUT);
  pinMode(PIN_TOUCH_XL, OUTPUT);
  digitalWrite(PIN_TOUCH_XL, LOW);
  pinMode(PIN_TOUCH_XR, OUTPUT);
  digitalWrite(PIN_TOUCH_XR, LOW);
  pinMode(PIN_TOUCH_YU, INPUT_PULLUP);
}

// Determine which screen half was touched by reading the resistive
// position. Temporarily reconfigures pins for analog reading, then
// restores detection mode. Returns true for the speed zone (right half),
// false for brightness zone (left half).
bool readTouchZone() {
  pinMode(PIN_TOUCH_XR, INPUT);
  pinMode(PIN_TOUCH_XL, INPUT);

  // Touch panel Y axis runs left-right in landscape.
  pinMode(PIN_TOUCH_YU, OUTPUT);
  digitalWrite(PIN_TOUCH_YU, HIGH);
  pinMode(PIN_TOUCH_YD, OUTPUT);
  digitalWrite(PIN_TOUCH_YD, LOW);
  delayMicroseconds(20);  // let the plate voltage settle
  int value = analogRead(PIN_TOUCH_XL);
  bool is_speed = value > 2048;

  touchDetectMode();
  return is_speed;
}

// On each new press, read which half of the screen was touched and
// cycle the corresponding setting. A short lockout absorbs bounce.
void checkTouch() {
  bool pressed = !digitalRead(PIN_TOUCH_YU);
  uint32_t now = millis();
  if (pressed && !touch_was_pressed && now - touch_last_cycle > 250) {
    if (readTouchZone()) {
      speed_idx = (speed_idx + 1) % NUM_SPEEDS;
      pan_speed = SPEED_LEVELS[speed_idx];
      sub_step = pan_speed / TARGET_FPS;
    } else {
      brightness_idx = (brightness_idx + 1) % NUM_BRIGHTNESS;
      brightness = BRIGHTNESS_LEVELS[brightness_idx];
    }
    touch_last_cycle = now;
  }
  touch_was_pressed = pressed;
}

// Set the backlight from the pass's elapsed time and remaining distance:
// ramp up over FADE_IN_S, hold at the current brightness level, ramp down
// over the final FADE_OUT_S (estimated from remaining columns at current
// speed). Squaring the ramp compensates for the eye's nonlinear brightness
// response so the fades look even.
void updateFade(uint32_t pass_start, uint32_t pos) {
  float t = (micros() - pass_start) * 1e-6f;
  float remaining_s = (max_pos - pos) / pan_speed;
  float k = min(min(t / FADE_IN_S, remaining_s / FADE_OUT_S), 1.0f);
  if (k < 0) k = 0;
  setBacklight(brightness * k * k);
}

void setup() {
  Serial.begin(115200);

  // Backlight starts dark; passes fade it up and down, hiding the resets.
  backlightBegin();

  // The ESP32 coprocessor shares the SPI bus with the SD card; keep it
  // deselected.
  pinMode(PIN_ESP_CS, OUTPUT);
  digitalWrite(PIN_ESP_CS, HIGH);

  // Set up the 8-bit parallel bus and hardware-reset the controller,
  // then send our own init sequence instead of the library's.
  tft.initSPI();
  sendInit();

  pinMode(PIN_TFT_TE, INPUT);

  analogReadResolution(12);
  touchDetectMode();

  if (!sd.begin(SdSpiConfig(PIN_SD_CS, SHARED_SPI, SD_SCK_MHZ(12)))) {
    fatal("SD card init failed");
  }
  if (!data.open(DATA_PATH, O_RDONLY)) {
    fatal("Could not open " DATA_PATH);
  }
  total_cols = data.fileSize() / COL_BYTES;
  if (total_cols <= (uint32_t)W) {
    fatal(DATA_PATH " is shorter than one screen");
  }
  max_pos = total_cols - W;

  last_report = millis();
}

void loop() {
  // Draw the starting screenful while the backlight is dark, then run
  // the strip once, scrolling in one direction only.
  for (int x = 0; x < W; x++) {
    loadColumn(x);
    blitColumn(x);
  }

  // Per-pass state: the strip column at the screen's left edge, plus
  // the timestamps that drive the fades and the frame schedule.
  uint32_t pos = 0;
  float sub_pos = 0.0f;
  uint32_t pass_start = micros();
  uint32_t next_frame = pass_start;

  while (pos < max_pos) {
    // Accumulate fractional scroll progress.
    sub_pos += sub_step;

    // Advance the scroll position when a full pixel boundary is crossed.
    if (sub_pos >= 1.0f) {
      // Convert the fractional progress to an integer number of pixels.
      uint32_t delta = (uint32_t)sub_pos;
      sub_pos -= delta;
      delta = min(delta, max_pos - pos);
      pos += delta;

      // Blit the columns entering on the right.
      bool first = true;
      for (int x = W - delta; x < W; x++) {
        // Read from the SD card before syncing, so the blanking window
        // is spent only on fast bus writes.
        loadColumn(pos + x);
        if (first) {
          // Scroll bumps latch at the frame boundary but writes land
          // immediately, so until then the entering column's line is
          // still mapped to the exiting edge. Writing inside vertical
          // blanking keeps the sweep from flashing it there.
          waitForBlanking();
          scroll = wrap(scroll + SCROLL_DIR * (int)delta);
          setScroll();
          first = false;
        }
        blitColumn(x);
      }
    }

    // Print the measured frame rate every 5 seconds.
    frames++;
    uint32_t now_ms = millis();
    if (now_ms - last_report >= 5000) {
      Serial.print(frames * 1000.0f / (now_ms - last_report), 1);
      Serial.println(" fps");
      frames = 0;
      last_report = now_ms;
    }

    // Spend the inter-frame wait polling touch and updating the
    // backlight ramp in small slices so the fades stay smooth.
    next_frame += FRAME_PERIOD_US;
    while (true) {
      checkTouch();
      updateFade(pass_start, pos);
      int32_t remaining = (int32_t)(next_frame - micros());
      if (remaining <= 0) break;
      delayMicroseconds(min(remaining, (int32_t)2000));
    }
    // If more than a full frame behind schedule, resync rather than
    // racing to catch up.
    if ((int32_t)(micros() - next_frame) > (int32_t)FRAME_PERIOD_US) {
      next_frame = micros();
    }
  }

  // Pass complete: settle on black, pause (still watching for touches),
  // then restart from the top.
  setBacklight(0);
  uint32_t hold_start = millis();
  while (millis() - hold_start < (uint32_t)(HOLD_BLACK_S * 1000)) {
    checkTouch();
    delay(20);
  }
}
