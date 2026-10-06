"""
packaging/windows/make_icon.py

Builds assets/ScheduleMaxing.ico -- the application's icon (window,
executable, installer, shortcuts) -- from the artwork in assets/icon/:

    assets/icon/schedule_maxing_<size>x<size>.png     for 16, 24, 32, 48, 64, 128 and 256

    python packaging/windows/make_icon.py

Run it again after changing any of those images; the .ico is committed, so a
build never needs to. Sizes below 256 px are stored as 32-bit bitmaps and
256 px as PNG: the layout every Windows version since Vista, and Tk,
PyInstaller and Inno Setup, read. The images must be 8-bit RGBA,
non-interlaced PNGs (what image editors export by default). Standard library
only, so no image package is needed.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_DIR = ROOT / "assets" / "icon"
SOURCE_NAME = "schedule_maxing_{size}x{size}.png"
OUTPUT = ROOT / "assets" / "ScheduleMaxing.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
Pixels = list[list[tuple[int, int, int, int]]]


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
    return (left, above, upper_left)[distances.index(min(distances))]


def read_png(path: Path) -> Pixels:
    """Rows of (red, green, blue, alpha), top row first, of an 8-bit RGBA non-interlaced PNG."""
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError(f"{path} is not a PNG file")
    position, header, compressed = len(PNG_SIGNATURE), None, b""
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        body = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            compressed += body
        position += 12 + length
    if header is None:
        raise ValueError(f"{path} has no image header")
    width, height, depth, color_type, _, _, interlace = header
    if (depth, color_type, interlace) != (8, 6, 0):
        raise ValueError(f"{path} must be an 8-bit RGBA, non-interlaced PNG")

    raw, stride, rows, previous = zlib.decompress(compressed), width * 4, [], bytearray(width * 4)
    for row in range(height):
        start = row * (stride + 1)
        filter_type, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for index in range(stride):
            left = line[index - 4] if index >= 4 else 0
            above = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 1:
                line[index] = (line[index] + left) & 0xFF
            elif filter_type == 2:
                line[index] = (line[index] + above) & 0xFF
            elif filter_type == 3:
                line[index] = (line[index] + (left + above) // 2) & 0xFF
            elif filter_type == 4:
                line[index] = (line[index] + _paeth(left, above, upper_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"{path} uses an unknown PNG filter ({filter_type})")
        rows.append([tuple(line[index:index + 4]) for index in range(0, stride, 4)])
        previous = line
    return rows


def _bitmap(rows: Pixels) -> bytes:
    size = len(rows)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = b"".join(bytes((blue, green, red, alpha)) for row in reversed(rows) for (red, green, blue, alpha) in row)
    mask_row = b"\x00" * (((size + 31) // 32) * 4)  # all visible: the alpha channel decides
    return header + pixels + mask_row * size


def build_icon(source_dir: Path = SOURCE_DIR, sizes: tuple[int, ...] = SIZES) -> bytes:
    images = []
    for size in sizes:
        path = source_dir / SOURCE_NAME.format(size=size)
        rows = read_png(path)
        if len(rows) != size or len(rows[0]) != size:
            raise ValueError(f"{path} is {len(rows[0])}x{len(rows)}, expected {size}x{size}")
        images.append(path.read_bytes() if size >= 256 else _bitmap(rows))
    offset = 6 + 16 * len(sizes)
    directory = b""
    for size, image in zip(sizes, images):
        edge = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", edge, edge, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    return struct.pack("<HHH", 0, 1, len(sizes)) + directory + b"".join(images)


if __name__ == "__main__":
    OUTPUT.write_bytes(build_icon())
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")
