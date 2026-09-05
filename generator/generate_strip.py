"""Generate a scrolling strip from a large astronomical mosaic.

Cuts one or more horizontal bands, 240 pixels tall, straight across a
deep-sky image and packs them into the raw format the PyPortal firmware
streams. The mosaic is already a single continuous picture, so the
generator only has to crop it.

Two modes:

  Single band (default):
    Cuts one band at a given vertical position. Good for targeting a
    specific region of the mosaic.

  Full coverage (--full):
    Tiles bands from top to bottom, covering the entire mosaic.
    Empty bands (black padding at the edges of non-rectangular images)
    are skipped, and leading/trailing black columns within each band
    are trimmed. A short black fade separates each band in the output.
    The result is a single .dat that scrolls through the whole image.

Outputs, both written to output/:

  <name>.dat -- raw big-endian RGB565 pixels, column-major (each screen
                column is one contiguous 480-byte record)
  <name>.png -- a preview decoded back out of the .dat, downscaled to a
                practical size. Because it is built from the actual
                bytes the PyPortal will read, it shows the real RGB565
                quantization and will expose a bad band position or a
                misordered chunk anywhere in the file

Usage:
    python generator/generate_strip.py                    # Andromeda, defaults
    python generator/generate_strip.py --list             # show all sources
    python generator/generate_strip.py --source carina
    python generator/generate_strip.py --scale 0.25 --y 0.45
    python generator/generate_strip.py --source carina --full

Source images are downloaded once into cache/ (resumable) and reused.
Then copy output/<name>.dat to the SD card as galaxy.dat, and
firmware/code.py to the CIRCUITPY drive.
"""

import argparse
import array
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import Image

# Astronomical mosaics are far larger than Pillow's decompression-bomb
# guard (~89.5 megapixels), which exists to stop malicious files rather
# than legitimate ones. Every source here is from a named observatory.
Image.MAX_IMAGE_PIXELS = None

# Fixed by firmware/code.py: a 240-px-tall strip at 2 bytes per pixel.
HEIGHT = 240
COL_BYTES = HEIGHT * 2

# Pan speeds the firmware cycles through on touch, used for the estimates.
SPEED_LEVELS = (10, 30, 60, 120, 200)

# Widest preview to write. Longer strips are downscaled by an integer
# factor rather than truncated: seeing the whole strip matters more than
# seeing it in full detail.
PREVIEW_MAX_COLS = 20000

# Columns converted per block. Keeps memory flat and gives progress.
CHUNK_COLS = 4096

# Black columns inserted between bands in --full mode (one screenful).
FADE_COLS = 320

# Brightness threshold (0-255) for content detection. Columns or bands
# whose mean grey level falls below this are treated as empty padding.
CONTENT_THRESHOLD = 10

# FAT32 maximum file size. CircuitPython's VfsFat only supports FAT32,
# so the .dat must stay under this limit.
FAT32_MAX_BYTES = 4 * 1024**3  # 4 GiB

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
OUTPUT_DIR = HERE / "output"


@dataclass(frozen=True)
class Source:
    """One downloadable mosaic. Dimensions are nominal, for planning; the
    real ones are read from the file itself at generation time."""

    title: str
    telescope: str
    width: int
    height: int
    url: str
    filename: str
    credit: str
    note: str = ""


# Every URL here was confirmed to return the stated file. Sizes in the
# comments are the download, not the decoded size in memory.
SOURCES: dict[str, Source] = {
    "andromeda": Source(
        title="M31 PHAT+PHAST",
        telescope="Hubble ACS/WFC3",
        width=42208,
        height=9870,
        url=(
            "https://mast.stsci.edu/api/latest/Download/file?uri=mast:OPO/"
            "product/STSCI_PR_2025-005/STSCI-H-p25005a-f-42208x9870.tif"
        ),
        filename="m31_phat_phast_42208x9870.tif",
        credit="NASA, ESA, B. Williams (UW), Z. Chen (UW), L. C. Johnson (Northwestern)",
        note="993 MB. 4.3:1 -- already strip-shaped, so one pass runs the whole disk.",
    ),
    "carina": Source(
        title="Cosmic Cliffs, NGC 3324",
        telescope="JWST NIRCam",
        width=14575,
        height=11227,
        url=(
            "https://mast.stsci.edu/api/latest/Download/file?uri=mast:OPO/"
            "product/STSCI_PR_2022-031/STSCI-J-p22031c-f-14575x11227.tif"
        ),
        filename="carina_cosmic_cliffs_14575x11227.tif",
        credit="NASA, ESA, CSA, STScI",
        note="134 MB. Small and quick -- a good end-to-end test of the pipeline.",
    ),
    "tarantula": Source(
        title="Tarantula Nebula (30 Doradus)",
        telescope="JWST NIRCam",
        width=14557,
        height=8418,
        url="https://esawebb.org/media/archives/images/original/weic2212a.tif",
        filename="tarantula_14557x8418.tif",
        credit="ESA/Webb, NASA, CSA, STScI",
        note="125 MB.",
    ),
    "vista25k": Source(
        title="Milky Way Centre, 25K reduction",
        telescope="ESO VISTA",
        width=25000,
        height=18833,
        url="https://cdn.eso.org/images/publicationtiff25k/eso1242a.tif",
        filename="vista_milky_way_centre_25k.tif",
        credit="ESO/VVV Survey/D. Minniti. Acknowledgement: Ignacio Toledo, Martin Kornmesser",
        note="1.5 GB. Needs pyvips on an 8 GB machine.",
    ),
    "vista40k": Source(
        title="Milky Way Centre, 40K reduction",
        telescope="ESO VISTA",
        width=40000,
        height=30132,
        url="https://cdn2.eso.org/images/publicationtiff40k/eso1242a.tif",
        filename="vista_milky_way_centre_40k.tif",
        credit="ESO/VVV Survey/D. Minniti. Acknowledgement: Ignacio Toledo, Martin Kornmesser",
        note="4.0 GB. Needs pyvips. The 8.8-gigapixel original is a .psb no library can open.",
    ),
    "rubin": Source(
        title="Cosmic Treasure Chest (Virgo Cluster)",
        telescope="Rubin LSSTCam",
        width=97943,
        height=51536,
        url="https://storage.noirlab.edu/media/archives/images/original/noirlab2521a.tif",
        filename="rubin_cosmic_treasure_chest.tif",
        credit="RubinObs/NOIRLab/SLAC/DOE/NSF/AURA",
        note="14.1 GB. Needs pyvips, and a lot of patience on the download.",
    ),
}

DEFAULT_SOURCE = "andromeda"


# --------------------------------------------------------------- download


def ensure_source(src: Source) -> Path:
    """Download the mosaic if it is not already cached, resuming a partial
    file rather than starting over. These run from 125 MB to 14 GB, so a
    dropped connection should not cost the whole transfer."""

    CACHE_DIR.mkdir(exist_ok=True)
    dest = CACHE_DIR / src.filename

    head = requests.head(src.url, allow_redirects=True, timeout=60)
    head.raise_for_status()
    total = int(head.headers.get("Content-Length", 0))
    have = dest.stat().st_size if dest.exists() else 0

    if total and have == total:
        print(f"Cached: {dest.name} ({have / 1e6:.0f} MB)")
        return dest
    if have > total > 0:
        # Cache is larger than the source: stale or corrupt, so start over.
        have = 0

    headers = {"Range": f"bytes={have}-"} if have else {}
    resp = requests.get(src.url, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()

    # A server that ignores the Range header sends 200 and the whole file.
    resuming = resp.status_code == 206
    if not resuming:
        have = 0

    action = "Resuming" if resuming else "Downloading"
    print(f"{action} {src.filename} ({total / 1e6:.0f} MB) ...")

    written = have
    next_report = written + 50e6
    with open(dest, "ab" if resuming else "wb") as f:
        for block in resp.iter_content(chunk_size=1 << 20):
            f.write(block)
            written += len(block)
            if written >= next_report:
                pct = f" ({100 * written / total:.0f}%)" if total else ""
                print(f"  {written / 1e6:.0f} MB{pct}", flush=True)
                next_report = written + 50e6

    print(f"  done, {written / 1e6:.0f} MB")
    return dest


# ------------------------------------------------------------- band crop


def extract_band(path: Path, scale: float, y_frac: float) -> tuple[Image.Image, int, int]:
    """Crop the horizontal band and scale it to exactly (out_w, HEIGHT).

    `scale` is a zoom factor: 1.0 takes a 240-px band at native resolution
    (maximum detail, a thin slice), while 0.25 takes a 960-px band and
    shrinks it (four times as much of the image, at a quarter the detail).
    `y_frac` places the band's centre as a fraction of the full height.

    pyvips is used when available because it reads only the rows it needs.
    Pillow has to decode the entire image first, which is fine up to about
    a gigapixel and impossible past it.
    """

    try:
        import pyvips
    except ImportError:
        pyvips = None

    band_h = max(1, round(HEIGHT / scale))

    if pyvips is not None:
        img = pyvips.Image.new_from_file(str(path), access="random")
        src_w, src_h = img.width, img.height
        y0 = _band_top(src_h, band_h, y_frac)
        region = img.crop(0, y0, src_w, min(band_h, src_h - y0))
        if region.bands > 3:
            region = region[0:3]
        elif region.bands == 1:
            region = region.bandjoin([region, region])
        band = Image.frombytes(
            "RGB", (region.width, region.height), region.write_to_memory()
        )
    else:
        _warn_if_memory_tight(path)
        img = Image.open(path)
        src_w, src_h = img.size
        y0 = _band_top(src_h, band_h, y_frac)
        band = img.crop((0, y0, src_w, min(y0 + band_h, src_h))).convert("RGB")
        img.close()

    # Scale the (now small) band to the exact strip height. Doing this
    # after the crop keeps the expensive resize off the full mosaic.
    out_w = max(1, round(src_w * scale))
    if band.size != (out_w, HEIGHT):
        band = band.resize((out_w, HEIGHT), Image.Resampling.LANCZOS)

    return band, src_w, src_h


def _band_top(src_h: int, band_h: int, y_frac: float) -> int:
    """Top edge of the band, clamped so it stays inside the image."""
    y0 = round(src_h * y_frac - band_h / 2)
    return max(0, min(y0, max(0, src_h - band_h)))


def _warn_if_memory_tight(path: Path) -> None:
    """Pillow decodes the whole mosaic to crop it. Say so before it tries."""
    with Image.open(path) as probe:
        w, h = probe.size
    need_gib = w * h * 3 / 1024**3
    if need_gib > 2.0:
        print(
            f"  note: Pillow will decode all {w * h / 1e6:.0f} megapixels "
            f"(~{need_gib:.1f} GiB of RAM).\n"
            f"        If this thrashes, install pyvips to stream instead:\n"
            f"        brew install vips && pip install pyvips"
        )


def apply_gamma(band: Image.Image, gamma: float) -> Image.Image:
    """Lift faint detail before the RGB565 conversion throws it away.

    Deep-sky images are mostly near-black, and RGB565 has only 32 levels
    per channel in red and blue -- so shadow detail that survives an 8-bit
    TIFF can band badly on the panel. Gamma above 1.0 brightens midtones
    and spreads that detail across more of the available levels.
    """
    if gamma == 1.0:
        return band
    table = [round(255 * (i / 255) ** (1 / gamma)) for i in range(256)]
    return band.point(table * 3)


# ------------------------------------------------- full-coverage helpers


def open_mosaic(path: Path):
    """Open the mosaic once, returning (image, width, height, is_pyvips).

    pyvips is preferred for large images because it can crop without
    decoding the whole file. Pillow works fine for smaller mosaics.
    """
    try:
        import pyvips
        img = pyvips.Image.new_from_file(str(path), access="random")
        return img, img.width, img.height, True
    except ImportError:
        _warn_if_memory_tight(path)
        img = Image.open(path)
        w, h = img.size
        return img, w, h, False


def crop_band_at(img, y0: int, band_h: int, src_w: int, src_h: int,
                 scale: float, is_pyvips: bool) -> Image.Image | None:
    """Crop a single band at an absolute pixel row and scale it to HEIGHT.

    Returns a PIL Image or None if the row is out of bounds."""
    actual_h = min(band_h, src_h - y0)
    if actual_h <= 0:
        return None

    out_w = max(1, round(src_w * scale))

    if is_pyvips:
        region = img.crop(0, y0, src_w, actual_h)
        if region.bands > 3:
            region = region[0:3]
        elif region.bands == 1:
            region = region.bandjoin([region, region])
        band = Image.frombytes(
            "RGB", (region.width, region.height), region.write_to_memory()
        )
    else:
        band = img.crop((0, y0, src_w, y0 + actual_h)).convert("RGB")

    if band.size != (out_w, HEIGHT):
        band = band.resize((out_w, HEIGHT), Image.Resampling.LANCZOS)

    return band


def find_content_bounds(band: Image.Image, threshold: int = CONTENT_THRESHOLD):
    """Find the horizontal extent of non-black content in a band.

    Converts to greyscale, thresholds, and uses getbbox() to find the
    bounding box of bright pixels. Returns (x_start, x_end) with x_end
    exclusive, or None if the band is empty.
    """
    grey = band.convert("L")
    mask = grey.point(lambda p: 255 if p > threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None
    return bbox[0], bbox[2]


def band_mean_brightness(band: Image.Image) -> float:
    """Mean grey level of the band (0-255)."""
    from PIL import ImageStat
    stat = ImageStat.Stat(band.convert("L"))
    return stat.mean[0]


# --------------------------------------------------------------- packing


def pack_columns(band: Image.Image, out, label: str = "") -> int:
    """Pack a band into column-major big-endian RGB565, writing to `out`.

    The firmware reads one column at a time, so columns must be
    contiguous. Transposing makes the row-major bytes come out in column
    order, and the ILI9341 takes the high byte first over its 8-bit bus.

    Returns the number of columns written.
    """
    width = band.width
    n_chunks = math.ceil(width / CHUNK_COLS)
    prefix = f"  {label} " if label else "  "

    for i, x0 in enumerate(range(0, width, CHUNK_COLS)):
        cw = min(CHUNK_COLS, width - x0)
        piece = band.crop((x0, 0, x0 + cw, HEIGHT))
        raw = piece.transpose(Image.Transpose.TRANSPOSE).tobytes()

        pixels = array.array(
            "H",
            (
                ((raw[j] & 0xF8) << 8)
                | ((raw[j + 1] & 0xFC) << 3)
                | (raw[j + 2] >> 3)
                for j in range(0, len(raw), 3)
            ),
        )
        if sys.byteorder == "little":
            pixels.byteswap()
        out.write(pixels.tobytes())

        print(f"{prefix}chunk {i + 1}/{n_chunks}", flush=True)

    return width


def write_black_columns(out, n: int) -> None:
    """Write n columns of black (zero) RGB565 pixels."""
    black = b"\x00" * COL_BYTES
    for _ in range(n):
        out.write(black)


def write_dat(band: Image.Image, dest: Path) -> int:
    """Pack a single band into a new .dat file. Returns total bytes."""
    with open(dest, "wb") as out:
        cols = pack_columns(band, out)
    return cols * COL_BYTES


def build_preview(dat_path: Path, dest: Path, length: int) -> int:
    """Decode the finished .dat back into a viewable image.

    Reading back the real bytes (rather than re-saving the band) is what
    makes this a check: if the column ordering, byte order, or band
    position is wrong, it is wrong here too and plainly visible.
    """

    factor = max(1, math.ceil(length / PREVIEW_MAX_COLS))
    pw = math.ceil(length / factor)
    note = f" ({factor}x downscaled)" if factor > 1 else ""
    print(f"Building preview{note} ...")

    # Expand RGB565 -> RGB888 through a lookup over all 16-bit values,
    # replicating each channel's high bits into its low bits.
    lut = [
        bytes(
            (
                ((v >> 8) & 0xF8) | (v >> 13),
                ((v >> 3) & 0xFC) | ((v >> 9) & 0x03),
                ((v << 3) & 0xF8) | ((v >> 2) & 0x07),
            )
        )
        for v in range(65536)
    ]

    # A whole number of preview columns per block, so nothing drifts.
    block_cols = factor * max(1, 4096 // factor)

    preview = Image.new("RGB", (pw, HEIGHT))
    with open(dat_path, "rb") as f:
        x = 0
        for c0 in range(0, length, block_cols):
            ncols = min(block_cols, length - c0)
            pixels = array.array("H")
            pixels.frombytes(f.read(ncols * COL_BYTES))
            if sys.byteorder == "little":
                pixels.byteswap()
            raw = b"".join(map(lut.__getitem__, pixels))

            # The .dat is column-major, so transpose back to ncols x HEIGHT.
            block = Image.frombytes("RGB", (HEIGHT, ncols), raw)
            block = block.transpose(Image.Transpose.TRANSPOSE)

            # The last block absorbs rounding so the preview fills fully.
            bw = pw - x if c0 + ncols >= length else ncols // factor
            if factor > 1:
                block = block.resize((bw, HEIGHT), Image.Resampling.BOX)
            preview.paste(block, (x, 0))
            x += bw

    preview.save(dest, optimize=True)
    return factor


# ------------------------------------------------------------------ main


def list_sources() -> None:
    width = max(len(k) for k in SOURCES)
    print("Available sources:\n")
    for key, s in SOURCES.items():
        gpx = s.width * s.height / 1e9
        star = " (default)" if key == DEFAULT_SOURCE else ""
        print(f"  {key:<{width}}  {s.title}{star}")
        print(f"  {'':<{width}}  {s.telescope} · {s.width:,} x {s.height:,} · {gpx:.2f} Gpx")
        if s.note:
            print(f"  {'':<{width}}  {s.note}")
        print()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cut a 240-px scrolling strip from a large space mosaic.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--list", action="store_true", help="list sources and exit")
    p.add_argument(
        "--source", default=DEFAULT_SOURCE, choices=sorted(SOURCES), help="which mosaic"
    )
    p.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="zoom factor; 1.0 = native detail, lower = more of the image, less detail",
    )
    p.add_argument(
        "--y",
        type=float,
        default=0.5,
        help="band centre as a fraction of image height (0.0 top, 1.0 bottom)",
    )
    p.add_argument(
        "--gamma",
        type=float,
        default=1.0,
        help="brighten midtones before RGB565; >1 lifts faint nebulosity",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="tile bands top-to-bottom, covering the entire mosaic",
    )
    p.add_argument(
        "--fade-cols",
        type=int,
        default=FADE_COLS,
        help="black columns inserted between bands in --full mode",
    )
    p.add_argument("--name", help="output basename (default: the source key)")
    return p.parse_args()


def print_run_times(total_cols: int) -> None:
    """Print estimated scroll times at each touch-selectable speed."""
    print("\nRun time at each touch-selectable speed:")
    for speed in SPEED_LEVELS:
        secs = total_cols / speed
        if secs < 3600:
            print(f"  {speed:>3} px/s   {secs / 60:.1f} min")
        elif secs < 86400:
            print(f"  {speed:>3} px/s   {secs / 3600:.1f} hr")
        else:
            print(f"  {speed:>3} px/s   {secs / 86400:.1f} days")


def run_single_band(args, src, source_file, dat_path, png_path) -> None:
    """Original single-band mode: cut one band at --y."""
    if not 0.0 <= args.y <= 1.0:
        sys.exit("--y must be between 0.0 and 1.0")

    print(f"\nCropping a {round(HEIGHT / args.scale)}px band at y={args.y:.2f} ...")
    band, src_w, src_h = extract_band(source_file, args.scale, args.y)
    band = apply_gamma(band, args.gamma)

    length = band.width
    print(f"Source {src_w:,} x {src_h:,} -> strip {length:,} x {HEIGHT}")
    print(f"Packing {length:,} columns ...")
    size = write_dat(band, dat_path)

    factor = build_preview(dat_path, png_path, length)

    print(f"\nWrote {dat_path} ({size / 1e6:.1f} MB)")
    print(f"Wrote {png_path} (preview{f', {factor}x downscaled' if factor > 1 else ''})")
    print_run_times(length)
    print(
        f"\nCheck {png_path.name} before deploying -- if the band misses the "
        f"interesting part,\nre-run with a different --y or a lower --scale."
        f"\nThen copy {dat_path.name} to the SD card as galaxy.dat, and "
        f"firmware/code.py to CIRCUITPY."
    )


def run_full_coverage(args, src, source_file, dat_path, png_path) -> None:
    """Full-coverage mode: tile bands top-to-bottom across the mosaic."""
    fade_cols = args.fade_cols

    img, src_w, src_h, is_pyvips = open_mosaic(source_file)
    band_h = max(1, round(HEIGHT / args.scale))
    n_bands = math.ceil(src_h / band_h)

    print(f"\nFull coverage: {n_bands} bands of {band_h}px across "
          f"{src_w:,} x {src_h:,} at scale {args.scale}")

    total_cols = 0
    bands_written = 0
    max_cols = FAT32_MAX_BYTES // COL_BYTES

    with open(dat_path, "wb") as out:
        for band_idx in range(n_bands):
            y0 = band_idx * band_h
            label = f"band {band_idx + 1}/{n_bands}"

            band = crop_band_at(img, y0, band_h, src_w, src_h,
                                args.scale, is_pyvips)
            if band is None:
                print(f"  {label}: out of bounds, skipping")
                continue

            mean_br = band_mean_brightness(band)
            if mean_br < CONTENT_THRESHOLD:
                print(f"  {label}: empty (mean brightness {mean_br:.1f}), skipping")
                continue

            bounds = find_content_bounds(band)
            if bounds is None:
                print(f"  {label}: no content after threshold, skipping")
                continue

            x_start, x_end = bounds
            if x_start > 0 or x_end < band.width:
                band = band.crop((x_start, 0, x_end, HEIGHT))
                print(f"  {label}: trimmed to columns {x_start:,}-{x_end:,} "
                      f"({band.width:,} cols)")
            else:
                print(f"  {label}: {band.width:,} cols")

            band = apply_gamma(band, args.gamma)

            # Check whether this band (plus fade) would exceed FAT32.
            needed = band.width + (fade_cols if bands_written > 0 else 0)
            if total_cols + needed > max_cols:
                remaining = max_cols - total_cols
                if bands_written > 0:
                    remaining -= fade_cols
                if remaining <= 0:
                    print(f"\n  FAT32 limit reached ({FAT32_MAX_BYTES / 1e9:.1f} GB). "
                          f"Stopping after {bands_written} bands.")
                    break
                band = band.crop((0, 0, remaining, HEIGHT))
                print(f"  truncated to {remaining:,} cols (FAT32 limit)")

            if bands_written > 0:
                write_black_columns(out, fade_cols)
                total_cols += fade_cols

            cols = pack_columns(band, out, label=label)
            total_cols += cols
            bands_written += 1

    if not is_pyvips:
        img.close()

    if bands_written == 0:
        sys.exit("No bands with content found. The mosaic may be empty.")

    size = total_cols * COL_BYTES
    factor = build_preview(dat_path, png_path, total_cols)

    print(f"\nFull coverage: {bands_written} bands, "
          f"{total_cols:,} total columns")
    print(f"Wrote {dat_path} ({size / 1e6:.1f} MB)")
    print(f"Wrote {png_path} (preview{f', {factor}x downscaled' if factor > 1 else ''})")
    print_run_times(total_cols)
    print(
        f"\nCheck {png_path.name} before deploying."
        f"\nThen copy {dat_path.name} to the SD card as galaxy.dat, and "
        f"firmware/code.py to CIRCUITPY."
    )


def main() -> None:
    args = parse_args()
    if args.list:
        list_sources()
        return

    if not 0.0 < args.scale <= 1.0:
        sys.exit("--scale must be greater than 0 and at most 1.0")

    src = SOURCES[args.source]
    name = args.name or args.source
    if args.full and not args.name:
        name = f"{args.source}_full"
    OUTPUT_DIR.mkdir(exist_ok=True)
    dat_path = OUTPUT_DIR / f"{name}.dat"
    png_path = OUTPUT_DIR / f"{name}.png"

    print(f"{src.title} -- {src.telescope}")
    print(f"Credit: {src.credit}\n")

    source_file = ensure_source(src)

    if args.full:
        run_full_coverage(args, src, source_file, dat_path, png_path)
    else:
        run_single_band(args, src, source_file, dat_path, png_path)


if __name__ == "__main__":
    main()
