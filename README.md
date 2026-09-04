# pyportal-galaxy

A visualization for the Adafruit PyPortal that scrolls a strip cut from a large astronomical mosaic across the screen, producing a slow, continuous journey through a galaxy or nebula.

It is the deep-sky counterpart to [pyportal-flyover](../pyportal-flyover), and reuses that project's display technique unchanged. The difference is in the source: instead of stitching an aerial corridor together from hundreds of map-tile requests, the imagery here is already a single continuous picture, so the generator only has to crop it.

The default source is the [Hubble mosaic of Andromeda](https://science.nasa.gov/missions/hubble/nasas-hubble-telescope-delivers-breathtaking-view-of-andromeda-galaxy/) (M31) from the PHAT and PHAST surveys — 42,208 x 9,870 pixels covering roughly 200 million individually resolved stars.

## Galaxy Visualization

Displaying the strip relies on two separate processes — cutting a strip out of a mosaic, and scrolling it across the PyPortal's screen.

### Image Generation

The strip is built by `generator/generate_strip.py`, which runs on your computer. It downloads the chosen mosaic once into `generator/cache/` (resumable, since these run from 125 MB to 14 GB), crops a 240-pixel-tall horizontal band across its full width, converts the band to RGB565, and writes it column-major to `generator/output/<source>.dat`.

Three flags shape the result:

| Flag | Effect |
| --- | --- |
| `--source` | Which mosaic to use. Run with `--list` to see all six. |
| `--scale` | Zoom. `1.0` cuts a 240-px band at native resolution — maximum detail, but a thin slice. `0.25` cuts a 960-px band and shrinks it, covering four times as much of the image at a quarter the detail. Lower values also shorten the strip. |
| `--y` | Where the band sits, as a fraction of the mosaic's height. `0.5` is the middle. |

A preview, `generator/output/<source>.png`, is written alongside the data. It is decoded back out of the finished `.dat` rather than from the source image, so it reflects the real RGB565 quantization and will expose a badly placed band or a misordered chunk before anything reaches the hardware. Strips wider than 20,000 columns are downscaled by an integer factor rather than truncated, so the preview always covers the whole strip.

Andromeda at the defaults produces a 42,208 x 240 strip: 20.3 MB, and 1.2 hours of screen time at the slowest pan speed.

### Image Display

The strip is displayed by `firmware/code.py`, which runs on the PyPortal and reads `galaxy.dat` from an SD card in the card slot. Since the data file is far larger than the PyPortal's RAM, it is never loaded whole — the script streams it one 480-byte column at a time into a reusable buffer, so memory use is constant no matter how long the strip is.

For smooth animation, the script drives the ILI9341 display controller directly and uses its hardware scrolling: after the first screenful is drawn, each frame only bumps the scroll register and writes the newly exposed columns, synced to vertical blanking for tear-free panning. Each completed pass fades the backlight out, resets, and fades back in.

Touch controls split the screen in half:

- **Left half** cycles brightness through 10%, 20%, 75%, and 100%.
- **Right half** cycles pan speed through 10, 30, 60, 120, and 200 px/s.

## Image Sources

All six are public mosaics from named observatories, verified downloadable. Sizes are the download, not the decoded size in memory.

| Source | Object | Telescope | Pixels | Download |
| --- | --- | --- | --- | --- |
| `andromeda` | M31, PHAT+PHAST | Hubble | 42,208 x 9,870 | 993 MB |
| `carina` | Cosmic Cliffs, NGC 3324 | JWST | 14,575 x 11,227 | 134 MB |
| `tarantula` | 30 Doradus | JWST | 14,557 x 8,418 | 125 MB |
| `vista25k` | Milky Way centre | ESO VISTA | 25,000 x 18,833 | 1.5 GB |
| `vista40k` | Milky Way centre | ESO VISTA | 40,000 x 30,132 | 4.0 GB |
| `rubin` | Virgo Cluster | Rubin | 97,943 x 51,536 | 14.1 GB |

Andromeda is the natural default because its 4.3:1 aspect ratio is already strip-shaped — a single horizontal band runs the length of the disk. The rest are roughly square, so a band crosses only a slice of them.

### A note on memory

Pillow has to decode an entire image before it can crop it, which costs `width x height x 3` bytes — 1.2 GiB for Andromeda, but 15 GiB for `rubin`. The three sources above a gigapixel therefore need [pyvips](https://github.com/libvips/pyvips), which reads only the rows the band actually covers:

```bash
brew install vips
.venv/bin/pip install pyvips
```

The generator uses pyvips automatically when it is importable and falls back to Pillow otherwise, so the smaller sources need nothing extra. It also warns before Pillow attempts a decode larger than 2 GiB.

## Repo Layout

```text
firmware/     code.py, copied to the CIRCUITPY drive
generator/    strip generator + requirements
              cache/  downloaded source mosaics (gitignored)
              output/ generated .dat and .png (gitignored)
```

## Usage

Set up a Python environment and install the generator's dependencies (one time):

```bash
python3 -m venv .venv
.venv/bin/pip install -r generator/requirements.txt
```

Generate the strip. This downloads the mosaic on first run and caches it:

```bash
.venv/bin/python generator/generate_strip.py --source andromeda
```

Check `generator/output/andromeda.png`. If the band misses the interesting part of the image, re-run with a different `--y`, or a lower `--scale` to cover more of it.

Then deploy in two steps. First, copy the strip data to the root of a FAT32-formatted micro SD card (ex. volume name GALAXY), renaming it to `galaxy.dat`, and insert the card into the PyPortal's SD slot:

```bash
cp generator/output/andromeda.dat /Volumes/GALAXY/galaxy.dat
```

Then copy the firmware to the PyPortal's CIRCUITPY drive, and create the `sd` folder the card gets mounted onto (one-time setup, required by CircuitPython):

```bash
cp firmware/code.py /Volumes/CIRCUITPY/
mkdir -p /Volumes/CIRCUITPY/sd
```

The PyPortal auto-reloads and starts scrolling. The SD card must be inserted before the PyPortal powers on, since the card is mounted at startup.

## Image Credits

- **Andromeda** — NASA, ESA, B. Williams (UW), Z. Chen (UW), L. C. Johnson (Northwestern)
- **Carina / Tarantula** — NASA, ESA, CSA, STScI (ESA/Webb for Tarantula)
- **Milky Way centre** — ESO/VVV Survey/D. Minniti. Acknowledgement: Ignacio Toledo, Martin Kornmesser
- **Virgo Cluster** — RubinObs/NOIRLab/SLAC/DOE/NSF/AURA
