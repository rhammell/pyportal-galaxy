# pyportal-galaxy

A visualization for the Adafruit PyPortal that scrolls through large astronomical mosaics, producing a slow, continuous journey across a galaxy or nebula. The generator tiles 240-pixel-tall bands from top to bottom across an entire mosaic, trims away empty regions, and stitches the bands into a single data file that can take days of continuous scrolling to complete. At the slowest pan speed, a strip of the Rubin Observatory's Virgo Cluster mosaic takes over 10 days to traverse.

This repository contains the source code for generating and displaying the galaxy imagery, and CAD files for a 3D-printable PyPortal stand.

For a complete description and step-by-step build tutorial, visit the [PyPortal Galaxy Viewer](https://www.hackster.io/rhammell/pyportal-galaxy-viewer-bf7b13) project on Hackster.io.

## Galaxy Visualization

Displaying the strip relies on two separate processes: cutting strips out of a mosaic, and scrolling them across the PyPortal's screen.

### Image Generation

The strip is built by `generator/generate_strip.py`, which runs on your computer. It downloads the chosen mosaic once into `generator/cache/` (resumable, since these range from 125 MB to 14 GB), converts bands to RGB565, and writes the result column-major to `generator/output/<source>.dat`.

The generator tiles 240-pixel bands from top to bottom, covering the entire mosaic in a single data file. Bands that fall on black padding at the edges of non-rectangular images are detected and skipped. Within each band, leading and trailing black columns are trimmed so scrolling jumps straight to the content. A short fade-to-black transition (320 columns by default, adjustable with `--fade-cols`) separates each band.

```bash
.venv/bin/python generator/generate_strip.py --source pandora
```

Run times at the slowest pan speed (10 px/s):

| Source | Bands | .dat Size | Scroll Time |
| --- | --- | --- | --- |
| `jades` | 36 | 214 MB | 12.4 hr |
| `carina` | 36 | 257 MB | 14.9 hr |
| `tarantula` | 36 | 245 MB | 14.9 hr |
| `pandora` | 56 | 483 MB | 1.2 days |
| `vista` | 126 | 2.3 GB | 5.9 days |
| `rubin` | 92 | 4.0 GB | 10.4 days |

Rubin reaches the FAT32 4 GB file size limit after 92 of its 215 bands, so the output is automatically capped there.

| Flag | Effect |
| --- | --- |
| `--source` | Which mosaic to use. Run with `--list` to see all six. |
| `--gamma` | Brighten midtones before RGB565 conversion. Values above 1.0 lift faint nebulosity. |

#### Preview

A preview PNG is written alongside the data file. It is decoded back out of the finished `.dat` rather than from the source image, so it reflects the real RGB565 quantization and will expose a badly placed band or a misordered chunk before anything reaches the hardware. Strips wider than 20,000 columns are downscaled by an integer factor so the preview always covers the whole strip.

### Image Display

The strip is displayed by the Arduino sketch `firmware/pyportal_galaxy/pyportal_galaxy.ino`, which runs on the PyPortal and reads `galaxy.dat` from an SD card in the card slot. Since the data file is far larger than the PyPortal's RAM, it is never loaded whole — the sketch streams it one 480-byte column at a time into a reusable buffer, so memory use is constant no matter how long the strip is.

For smooth animation, the sketch drives the ILI9341 display controller directly and uses its hardware scrolling: after the first screenful is drawn, each frame only bumps the scroll register and writes the newly exposed columns, synced to vertical blanking for tear-free panning. Each completed pass fades the backlight out, resets, and fades back in.

Touch controls split the screen in half:

- **Left half** cycles brightness through 10%, 20%, 75%, and 100%.
- **Right half** cycles pan speed through 10, 120, 240, 480, and 800 px/s.

## Image Sources

All six are public mosaics from named observatories, verified downloadable. Sizes are the download, not the decoded size in memory.

| Source | Object | Telescope | Pixels | Download |
| --- | --- | --- | --- | --- |
| `carina` | Cosmic Cliffs, NGC 3324 | JWST | 14,575 x 8,441 | 137 MB |
| `tarantula` | 30 Doradus | JWST | 14,557 x 8,418 | 125 MB |
| `pandora` | Pandora's Cluster, Abell 2744 | JWST | 17,644 x 13,422 | 178 MB |
| `jades` | GOODS-South deep field | JWST | 12,097 x 8,482 | 127 MB |
| `vista` | Milky Way centre | ESO VISTA | 40,000 x 30,132 | 4.0 GB |
| `rubin` | Virgo Cluster | Rubin | 97,943 x 51,536 | 14.1 GB |

Pandora's Cluster is the default because it gives the longest scroll of the sources that need no extra dependencies, and its fully rectangular frame leaves no empty edges to trim. Regardless of the original aspect ratio, every source produces a complete traversal of the mosaic.

### A note on memory

Pillow decodes an entire image before cropping, which costs `width x height x 3` bytes: 0.7 GiB for Pandora's Cluster, but 15 GiB for Rubin. The two sources above a gigapixel (`vista`, `rubin`) therefore need [pyvips](https://github.com/libvips/pyvips), which reads only the rows each band covers. The generator uses pyvips automatically when it is importable and falls back to Pillow otherwise, so the smaller sources need nothing extra.

### FAT32 file size limit

The PyPortal's SD card must be FAT32 formatted, which imposes a 4 GB maximum file size. The generator monitors the output size and stops adding bands once the limit is reached. For most sources the entire mosaic fits comfortably. Rubin is the exception, capping at 92 of 215 bands, which still provides over 10 days of scroll time at the slowest speed.

## Repo Layout

```text
firmware/     pyportal_galaxy/ Arduino sketch, uploaded to the PyPortal
generator/    strip generator + requirements
              cache/   downloaded source mosaics (gitignored)
              output/  generated .dat and .png files (gitignored)
                       <source>.dat / .png       — generated strip
cad/          stand design (src/ = editable CAD, export/ = printable STL exports)
```

## Usage

### 1. Install dependencies (one time)

```bash
python3 -m venv .venv
.venv/bin/pip install -r generator/requirements.txt
```

For sources larger than a gigapixel (`vista`, `rubin`), pyvips is also required:

```bash
brew install vips          # macOS; see libvips docs for other platforms
.venv/bin/pip install pyvips
```

### 2. Generate a strip

```bash
.venv/bin/python generator/generate_strip.py --source pandora
```

The mosaic is downloaded on first run and cached in `generator/cache/`. Check the preview PNG in `generator/output/` before deploying.

### 3. Deploy to hardware

Copy the strip data to the root of a FAT32-formatted micro SD card (e.g. volume name GALAXY), renaming it to `galaxy.dat`, and insert the card into the PyPortal's SD slot:

```bash
cp generator/output/pandora.dat /Volumes/GALAXY/galaxy.dat
```

Upload the firmware with the Arduino IDE. One-time setup:

1. In **Settings**, add `https://adafruit.github.io/arduino-board-index/package_adafruit_index.json` to "Additional boards manager URLs".
2. In **Boards Manager**, install **Arduino SAMD Boards**, then **Adafruit SAMD Boards**.
3. In **Library Manager**, install **Adafruit ILI9341**, **Adafruit GFX Library**, and **SdFat - Adafruit Fork**.

Then open `firmware/pyportal_galaxy/pyportal_galaxy.ino`, select **Tools → Board → Adafruit SAMD → Adafruit PyPortal M4**, pick the PyPortal's port, and click **Upload**. If the board is still running CircuitPython, or the port doesn't appear, double-tap the reset button to enter the bootloader first.

The PyPortal starts scrolling as soon as the upload finishes. The SD card must be inserted before the PyPortal powers on, since the card is mounted at startup. The measured frame rate, and any SD card or file errors, are printed to the Serial Monitor at 115200 baud.

## Image Credits

- **Carina / Tarantula** — NASA, ESA, CSA, STScI (ESA/Webb for Tarantula)
- **Pandora's Cluster** — NASA, ESA, CSA, Ivo Labbe (Swinburne), Rachel Bezanson (University of Pittsburgh); Image Processing: Alyssa Pagan (STScI)
- **JADES** — NASA, ESA, CSA, Brant Robertson (UC Santa Cruz), Ben Johnson (CfA), Sandro Tacchella (Cambridge), Marcia Rieke (University of Arizona), Daniel Eisenstein (CfA); Image Processing: Alyssa Pagan (STScI)
- **Milky Way centre** — ESO/VVV Survey/D. Minniti. Acknowledgement: Ignacio Toledo, Martin Kornmesser
- **Virgo Cluster** — RubinObs/NOIRLab/SLAC/DOE/NSF/AURA
