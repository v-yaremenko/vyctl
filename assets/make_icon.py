"""Generate vyctl.ico -- no third-party imaging library.

The mark is the app's own layout: a column of session status dots beside the console
lines they drive.  Drawn at 4x and box-filtered down, which is all the antialiasing a
shape this simple needs.

Sizes up to 128 are stored as 32bpp BMP; the 256 entry is PNG, because Vista+ skips a
BMP that large and silently falls back to 128.
"""
import struct
import zlib

BG     = (0x16, 0x1B, 0x22)   # near-black, matches a dark terminal ground
GREEN  = (0x3F, 0xB9, 0x50)   # Working
AMBER  = (0xD2, 0x99, 0x22)   # Idle
GREY   = (0x6E, 0x76, 0x81)   # Inactive
BAR    = (0x8B, 0x94, 0x9E)
BAR_HI = (0xC9, 0xD1, 0xD9)
SS = 4  # supersample factor


def blank(n):
    return [[(0, 0, 0, 0)] * n for _ in range(n)]


def fill_rrect(buf, x0, y0, x1, y1, r, col):
    n = len(buf)
    for y in range(max(0, int(y0)), min(n, int(y1) + 1)):
        for x in range(max(0, int(x0)), min(n, int(x1) + 1)):
            cx = min(max(x, x0 + r), x1 - r)
            cy = min(max(y, y0 + r), y1 - r)
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                buf[y][x] = col + (255,)


def fill_circle(buf, cx, cy, r, col):
    n = len(buf)
    for y in range(max(0, int(cy - r)), min(n, int(cy + r) + 1)):
        for x in range(max(0, int(cx - r)), min(n, int(cx + r) + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                buf[y][x] = col + (255,)


def draw(size):
    n = size * SS
    buf = blank(n)
    fill_rrect(buf, 0, 0, n - 1, n - 1, n * 0.19, BG)

    rows = (0.315, 0.5, 0.685)
    dots = (GREEN, AMBER, GREY)
    if size <= 24:
        # Too few pixels for bars -- the three dots alone still read as "sessions".
        for row, col in zip(rows, dots):
            fill_circle(buf, n * 0.5, n * row, n * 0.105, col)
    else:
        for i, (row, col) in enumerate(zip(rows, dots)):
            fill_circle(buf, n * 0.275, n * row, n * 0.062, col)
            h = n * 0.055
            w = n * (0.78 if i == 0 else 0.70 if i == 1 else 0.58)
            fill_rrect(buf, n * 0.42, n * row - h / 2, w, n * row + h / 2,
                       h / 2, BAR_HI if i == 0 else BAR)

    out = blank(size)
    f = SS * SS
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(SS):
                for dx in range(SS):
                    pr, pg, pb, pa = buf[y * SS + dy][x * SS + dx]
                    r += pr * pa; g += pg * pa; b += pb * pa; a += pa
            out[y][x] = ((r // a, g // a, b // a, a // f) if a else (0, 0, 0, 0))
    return out


def bmp(px):
    """32bpp bottom-up BGRA plus the legacy AND mask Windows still expects."""
    size = len(px)
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = b"".join(
        b"".join(struct.pack("<BBBB", p[2], p[1], p[0], p[3]) for p in px[y])
        for y in range(size - 1, -1, -1)
    )
    stride = ((size + 31) // 32) * 4
    return hdr + xor + b"\x00" * (stride * size)


def png(px):
    """Top-down RGBA PNG, filter 0 on every scanline."""
    size = len(px)
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("<BBBB", *p) for p in row) for row in px
    )

    def chunk(tag, body):
        c = tag + body
        return struct.pack(">I", len(body)) + c + struct.pack(">I", zlib.crc32(c))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico(path, sizes):
    imgs = [(png if s >= 256 else bmp)(draw(s)) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for s, data in zip(sizes, imgs):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(path, "wb") as fh:
        fh.write(out + b"".join(imgs))
    return offset


sizes = [16, 24, 32, 48, 64, 128, 256]
total = ico(r"D:\workspace\tools\claude_terminals\vyctl.ico", sizes)
print("wrote vyctl.ico:", total, "bytes,", len(sizes), "sizes", sizes)
