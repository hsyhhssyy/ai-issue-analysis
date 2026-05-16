#!/usr/bin/env python3
"""Generate the test error screenshot PNG (stdlib only, no Pillow needed)."""
import struct
import zlib
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "error-screenshot.png"

W, H = 800, 500
BG = (30, 30, 30)
WHITE = (220, 220, 220)
RED = (230, 80, 80)
YELLOW = (220, 200, 80)
BLUE = (80, 160, 230)
GRAY = (50, 50, 50)
GREEN = (80, 200, 80)
DARK_RED_BG = (40, 15, 15)
LINE_NUM_GRAY = (60, 60, 60)
TITLE_BAR = (40, 40, 40)

# Simulated text layout (start x, end x, y range, color)
TEXT_BLOCKS = [
    # Title bar text
    (10, 300, 5, 25, (150, 150, 150)),  # "Terminal"
    # Red error message
    (60, 500, 170, 200, RED),
    # Blue file path
    (60, 400, 90, 115, BLUE),
    # Stack trace lines (light gray)
    (60, 520, 130, 160, WHITE),
    # Yellow warning
    (60, 350, 320, 345, YELLOW),
    # Green prompt
    (60, 220, 400, 420, GREEN),
    # Some dim text lines
    (60, 300, 60, 85, (100, 100, 100)),
    (60, 300, 220, 245, (120, 120, 120)),
    (60, 300, 250, 275, (120, 120, 120)),
    (60, 300, 350, 375, (140, 140, 140)),
]

pixels: list[int] = []

for y in range(H):
    for x in range(W):
        # Default: dark terminal background
        px = BG

        # Title bar
        if y < 30:
            px = TITLE_BAR
            if 12 < y < 22 and x == W - 45:
                px = (200, 60, 60)  # red close dot

        # Dark red highlighted error area
        if 160 < y < 205:
            px = DARK_RED_BG

        # Line number column
        if x < 45 and y >= 30:
            line_no = (y - 30) // 15 + 1
            if 10 <= x <= 32 and y % 15 < 12:
                px = LINE_NUM_GRAY

        # Horizontal separator lines (subtle)
        if y % 45 == 0 and y > 30 and x < W:
            px = (40, 40, 40)

        # Color text blocks
        for sx, ex, sy, ey, color in TEXT_BLOCKS:
            if sx <= x <= ex and sy <= y <= ey:
                # Create a stripped text effect
                dx = (x - sx) % 8
                dy = (y - sy) % 16
                if dx < 6 and dy < 12:
                    px = color
                else:
                    px = BG

        pixels.extend((*px, 255))


def create_png() -> bytes:
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)

    raw = b''
    for y in range(H):
        raw += b'\x00'
        for x in range(W):
            idx = (y * W + x) * 4
            raw += struct.pack("BBBB", pixels[idx], pixels[idx+1], pixels[idx+2], pixels[idx+3])

    def chunk(kind: bytes, data: bytes) -> bytes:
        c = kind + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


OUTPUT.write_bytes(create_png())
print(f"Generated: {OUTPUT} ({OUTPUT.stat().st_size} bytes)")
