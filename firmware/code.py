"""PyPortal galaxy: tear-free panning via ILI9341 hardware scroll.

Scrolls a strip cut from a large astronomical mosaic across the screen,
so the display slowly traverses a galaxy, a nebula, or a field of them.

Instead of letting displayio repaint all 76,800 pixels per frame (slow,
visible wipe/tearing), this drives the display controller directly:

  - takes over the 8-bit parallel bus from displayio
  - sends the same ILI9341 init sequence the stock firmware uses
  - draws the first screenful once, then each frame only bumps the
    panel's vertical-scroll register and writes the few newly exposed
    columns (~1 KB) into the panel's own frame memory

The ILI9341's scroll axis is its long (320 px) axis, which in the
PyPortal's landscape mounting is exactly the horizontal pan direction,
and the scroll wraps around -- the panel's frame memory acts as the
ring buffer.

The strip is never held in memory: it is read one 480-byte column at a
time straight off the SD card, so RAM use is constant whether the file
is 20 MB or 2 GB.

Touching the left half of the screen cycles brightness; the right half
cycles pan speed.

Requires galaxy.dat: raw big-endian RGB565 pixels, column-major (each
column is one contiguous 480-byte record), stored on the SD card and
produced by generator/generate_strip.py.
"""

import analogio
import struct
import time

import board
import digitalio
import displayio
import pwmio
import sdcardio
import storage

try:
    from paralleldisplaybus import ParallelBus  # CircuitPython 9+
except ImportError:
    from displayio import ParallelBus

# Strip image data, read from the SD card.
DATA_PATH = "/sd/galaxy.dat"

# Pan speed in pixels per second and loop rate. The loop runs at
# TARGET_FPS for smooth fades/touch; the accumulator advances the
# scroll by pan_speed/TARGET_FPS pixels per frame (fractional).
SPEED_LEVELS = (10, 30, 60, 120, 200)
TARGET_FPS = 50

# Backlight fade durations at the start and end of each pass, and the
# pause on black in between passes.
FADE_IN_S = 0.5
FADE_OUT_S = 0.5
HOLD_BLACK_S = 1.0

# Steady-state backlight levels (0.0-1.0); each screen touch cycles to the
# next, starting on the first. Fades ramp between black and the current level.
BRIGHTNESS_LEVELS = (0.1, 0.20, 0.75, 1.0)

# Mounting orientation. Landscape: imagery enters on the right. Portrait
# (landscape-left edge up): output is rotated 180 so imagery enters on top.
PORTRAIT = False

# Scroll register direction per MADCTL line order: landscape (0xA8)
# decrements, portrait (0x68) increments. If it pans with garbage, negate.
SCROLL_DIR = 1 if PORTRAIT else -1

# Screen width and height.
W = 320
H = 240

# Number of bytes per column.
COL_BYTES = H * 2

# Detach displayio from the hardware so we can drive the bus ourselves.
displayio.release_displays()

# Rebuild the 8-bit parallel bus with the pins displayio normally uses,
# then hardware-reset the controller and give it time to come back up.
bus = ParallelBus(
    data0=board.LCD_DATA0,
    command=board.TFT_RS,  # PB05; board.TFT_DC wrongly aliases the WR pin
    chip_select=board.TFT_CS,
    write=board.TFT_WR,
    read=board.TFT_RD,
    reset=board.TFT_RESET,
)
bus.reset()
time.sleep(0.1)

# Stock PyPortal init sequence (from the CircuitPython board definition).
INIT = (
    (0xEF, b"\x03\x80\x02", 0),
    (0xCF, b"\x00\xc1\x30", 0),
    (0xED, b"\x64\x03\x12\x81", 0),
    (0xE8, b"\x85\x00\x78", 0),
    (0xCB, b"\x39\x2c\x00\x34\x02", 0),
    (0xF7, b"\x20", 0),
    (0xEA, b"\x00\x00", 0),
    (0xC0, b"\x23", 0),  # Power control VRH
    (0xC1, b"\x10", 0),  # Power control SAP/BT
    (0xC5, b"\x3e\x28", 0),  # VCM control
    (0xC7, b"\x86", 0),  # VCM control 2
    # MADCTL: BGR color order; 0x68 flips both address-order bits relative
    # to 0xA8, i.e. a 180-degree rotation in panel memory for portrait.
    (0x36, b"\x68" if PORTRAIT else b"\xa8", 0),
    (0x37, b"\x00\x00", 0),  # Scroll start = 0
    (0x3A, b"\x55", 0),  # 16 bits per pixel
    (0xB1, b"\x00\x18", 0),  # Frame rate control
    # Widen the vertical porches so the blanking window after each TE
    # pulse is long enough (~2.5 ms) to bump the scroll register and
    # write a full column before the panel starts scanning again.
    (0xB5, b"\x10\x30\x0a\x14", 0),  # VFP=16, VBP=48 lines
    (0x35, b"\x00", 0),  # Tearing-effect line on (V-blank pulses only)
    (0xB6, b"\x08\xa2\x27", 0),  # Display function control
    (0xF2, b"\x00", 0),  # 3Gamma off
    (0x26, b"\x01", 0),  # Gamma curve
    (0xE0, b"\x0f\x31\x2b\x0c\x0e\x08\x4e\xf1\x37\x07\x10\x03\x0e\x09\x00", 0),
    (0xE1, b"\x00\x0e\x14\x03\x11\x07\x31\xc1\x48\x08\x0f\x0c\x31\x36\x0f", 0),
    (0x33, b"\x00\x00\x01\x40\x00\x00", 0),  # Scroll area = full 320 lines
    (0x11, b"", 120),  # Exit sleep
    (0x29, b"", 120),  # Display on
)

# Send each init command, honoring the required post-command delays.
for cmd, cmd_data, delay_ms in INIT:
    bus.send(cmd, cmd_data)
    if delay_ms:
        time.sleep(delay_ms / 1000)

# Mount the SD card (shares the main SPI bus, selected by SD_CS).
sd = sdcardio.SDCard(board.SPI(), board.SD_CS)
storage.mount(storage.VfsFat(sd), "/sd")

# Open the image data, size it in columns, and set up a reusable buffer
# that holds one column of pixels at a time.
data = open(DATA_PATH, "rb")
data.seek(0, 2)
total_cols = data.tell() // COL_BYTES
colbuf = bytearray(COL_BYTES)

# Full-height row address window.
ROW_WINDOW = struct.pack(">HH", 0, H - 1)  # full-height row address window
scroll = 0  # current VSCRSAD register value


def set_scroll():
    bus.send(0x37, struct.pack(">H", scroll))


def load_column(world_col):
    """Read one strip column from disk into the column buffer."""
    data.seek(world_col * COL_BYTES)
    data.readinto(colbuf)


def blit_column(screen_x):
    """Write the column buffer into panel memory so that it appears at
    screen_x under the current scroll value."""
    xw = (screen_x + SCROLL_DIR * scroll) % W
    bus.send(0x2A, struct.pack(">HH", xw, xw))  # column address
    bus.send(0x2B, ROW_WINDOW)  # row address
    bus.send(0x2C, colbuf)  # memory write


# The tearing-effect pin pulses high during vertical blanking (~66 Hz);
# scroll bumps and column writes happen inside that scan-free window.
te = digitalio.DigitalInOut(board.TFT_TE)
te.switch_to_input()


def wait_for_blanking():
    """Block until the next rising edge of the tearing-effect signal."""
    while te.value:
        pass
    while not te.value:
        pass


# Backlight starts dark; passes fade it up and down, hiding the resets.
backlight = pwmio.PWMOut(board.TFT_BACKLIGHT, frequency=25000, duty_cycle=0)

# Touch sensing: hold the resistive panel's X plate low and pull the Y
# sense line up; a press shorts the plates and pulls the sense line low.
touch_xl = digitalio.DigitalInOut(board.TOUCH_XL)
touch_xl.switch_to_output(value=False)
touch_xr = digitalio.DigitalInOut(board.TOUCH_XR)
touch_xr.switch_to_output(value=False)
touch_sense = digitalio.DigitalInOut(board.TOUCH_YU)
touch_sense.switch_to_input(pull=digitalio.Pull.UP)

# Brightness selection state.
brightness_idx = 0
brightness = BRIGHTNESS_LEVELS[brightness_idx]

# Speed selection state.
speed_idx = 0
pan_speed = SPEED_LEVELS[speed_idx]
sub_step = pan_speed / TARGET_FPS

# Touch detection state.
touch_was_pressed = False
touch_last_cycle = 0.0


def read_touch_zone():
    """Determine which screen half was touched by reading the resistive
    position. Temporarily reconfigures pins for analog reading, then
    restores detection mode. Returns True for the speed zone (right in
    landscape, bottom in portrait), False for brightness zone."""
    global touch_xl, touch_xr, touch_sense
    touch_xl.deinit()
    touch_xr.deinit()
    touch_sense.deinit()

    if PORTRAIT:
        # Touch panel Y axis runs top-bottom in portrait.
        yu = digitalio.DigitalInOut(board.TOUCH_YU)
        yu.switch_to_output(value=True)
        yd = digitalio.DigitalInOut(board.TOUCH_YD)
        yd.switch_to_output(value=False)
        sense = analogio.AnalogIn(board.TOUCH_XL)
        value = sense.value
        sense.deinit()
        yu.deinit()
        yd.deinit()
        is_speed = value > 32768
    else:
        # Touch panel Y axis runs left-right in landscape.
        yu = digitalio.DigitalInOut(board.TOUCH_YU)
        yu.switch_to_output(value=True)
        yd = digitalio.DigitalInOut(board.TOUCH_YD)
        yd.switch_to_output(value=False)
        sense = analogio.AnalogIn(board.TOUCH_XL)
        value = sense.value
        sense.deinit()
        yu.deinit()
        yd.deinit()
        is_speed = value > 32768

    # Restore detection mode.
    touch_xl = digitalio.DigitalInOut(board.TOUCH_XL)
    touch_xl.switch_to_output(value=False)
    touch_xr = digitalio.DigitalInOut(board.TOUCH_XR)
    touch_xr.switch_to_output(value=False)
    touch_sense = digitalio.DigitalInOut(board.TOUCH_YU)
    touch_sense.switch_to_input(pull=digitalio.Pull.UP)
    return is_speed


def check_touch():
    """On each new press, read which half of the screen was touched and
    cycle the corresponding setting. A short lockout absorbs bounce."""
    global brightness_idx, brightness, speed_idx, pan_speed, sub_step
    global touch_was_pressed, touch_last_cycle
    pressed = not touch_sense.value
    now = time.monotonic()
    if pressed and not touch_was_pressed and now - touch_last_cycle > 0.25:
        if read_touch_zone():
            speed_idx = (speed_idx + 1) % len(SPEED_LEVELS)
            pan_speed = SPEED_LEVELS[speed_idx]
            sub_step = pan_speed / TARGET_FPS
        else:
            brightness_idx = (brightness_idx + 1) % len(BRIGHTNESS_LEVELS)
            brightness = BRIGHTNESS_LEVELS[brightness_idx]
        touch_last_cycle = now
    touch_was_pressed = pressed


# Pan range and frame interval.
max_pos = total_cols - W
frame_period = 1 / TARGET_FPS

# State for the periodic fps report.
frames = 0
last_report = time.monotonic()


def update_fade(pass_start, pos):
    """Set the backlight from the pass's elapsed time and remaining
    distance: ramp up over FADE_IN_S, hold at the current brightness
    level, ramp down over the final FADE_OUT_S (estimated from remaining
    columns at current speed). Squaring the ramp compensates for the
    eye's nonlinear brightness response so the fades look even."""
    t = time.monotonic() - pass_start
    remaining_s = (max_pos - pos) / pan_speed
    k = min(t / FADE_IN_S, remaining_s / FADE_OUT_S, 1.0)
    if k < 0:
        k = 0
    backlight.duty_cycle = int(65535 * brightness * k * k)


while True:
    # Draw the starting screenful while the backlight is dark, then run
    # the strip once, scrolling in one direction only.
    for x in range(W):
        load_column(x)
        blit_column(x)

    # Per-pass state: the strip column at the screen's left edge, plus
    # the timestamps that drive the fades and the frame schedule.
    pos = 0
    sub_pos = 0.0
    pass_start = time.monotonic()
    next_frame = pass_start

    while pos < max_pos:
        # Accumulate fractional scroll progress
        sub_pos += sub_step

        # Advance the scroll position when a full pixel boundary is crossed.
        if sub_pos >= 1.0:
            # Convert the fractional progress to an integer number of pixels.
            delta = int(sub_pos)
            sub_pos -= delta
            delta = min(delta, max_pos - pos)
            pos += delta

            # Blit the columns entering on the right.
            first = True
            for x in range(W - delta, W):  # columns entering on the right
                # Read from the SD card before syncing, so the blanking window
                # is spent only on fast bus writes.
                load_column(pos + x)
                if first:
                    # Scroll bumps latch at the frame boundary but writes land
                    # immediately, so until then the entering column's line is
                    # still mapped to the exiting edge. Writing inside vertical
                    # blanking keeps the sweep from flashing it there.
                    wait_for_blanking()
                    scroll = (scroll + SCROLL_DIR * delta) % W
                    set_scroll()
                    first = False
                blit_column(x)

        # Print the measured frame rate every 5 seconds.
        frames += 1
        now = time.monotonic()
        if now - last_report >= 5:
            print(f"{frames / (now - last_report):.1f} fps")
            frames = 0
            last_report = now

        # Spend the inter-frame wait polling touch and updating the
        # backlight ramp in small slices so the fades stay smooth.
        next_frame += frame_period
        while True:
            check_touch()
            update_fade(pass_start, pos)
            remaining = next_frame - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.02))
        # If more than a full frame behind schedule, resync rather than
        # racing to catch up.
        if time.monotonic() - next_frame > frame_period:
            next_frame = time.monotonic()

    # Pass complete: settle on black, pause (still watching for touches),
    # then restart from the top.
    backlight.duty_cycle = 0
    hold_end = time.monotonic() + HOLD_BLACK_S
    while time.monotonic() < hold_end:
        check_touch()
        time.sleep(0.02)
