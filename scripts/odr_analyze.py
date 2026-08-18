#!/usr/bin/env python3
"""Offline analysis of an `odr_capture.py` session: what was said, and what the blobs are.

Two jobs, both done without a keyboard attached, so a capture can be taken in one sitting
and picked apart afterwards.

**What was said.** Tallies every frame by resolved method name and direction. This is the
map of the display surface: the point of a capture is to find which of the service's
methods Komplete Kontrol actually uses to draw, out of the 437 names its registry
advertises.

**What the blobs are.** The registry carries an `is_rgb565` flag next to its asset symbols,
which says image payloads are raw 16-bit pixels rather than an encoded format. Raw pixels
carry no dimensions, so this recovers them arithmetically: a blob of N bytes is N/2 pixels,
and every way of factoring that into a plausible width and height is a candidate. Each
candidate is written out as a PNG. The right one is obvious on sight and the wrong ones are
visibly sheared, which is a faster oracle than reasoning about it.

PNGs are written with zlib and struct alone, so this keeps the project's no-dependencies
rule: nothing here needs Pillow.

Usage:
    ./scripts/odr_analyze.py captures/<name>
    ./scripts/odr_analyze.py captures/<name> --blob <sha1-prefix>   # just this one
    ./scripts/odr_analyze.py --selftest
"""

import argparse
import collections
import glob
import json
import os
import struct
import sys
import tempfile
import zlib

# A blob has to factor into something a screen could plausibly be. These bounds exist to
# keep the candidate list short enough to eyeball, not because anything outside them is
# impossible; widen them if a real asset falls outside.
MIN_WIDTH, MAX_WIDTH = 32, 2048
MIN_HEIGHT, MAX_HEIGHT = 16, 1024
MAX_CANDIDATES = 8


def write_png(path, width, height, rgb_rows):
    """Minimal RGB8 PNG writer: signature, IHDR, IDAT, IEND."""
    raw = b"".join(b"\x00" + row for row in rgb_rows)

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(chunk(b"IEND", b""))


def rgb565_rows(payload, width, height, big_endian=False):
    """Expand raw RGB565 to 8-bit RGB rows.

    The low bits are replicated into the high ones when widening (`x << 3 | x >> 2`)
    rather than zero-filled, so full-scale input stays full-scale output and white does
    not come out slightly grey.
    """
    order = ">H" if big_endian else "<H"
    rows = []
    for y in range(height):
        row = bytearray()
        base = y * width * 2
        for x in range(width):
            (value,) = struct.unpack_from(order, payload, base + x * 2)
            r, g, b = (value >> 11) & 0x1F, (value >> 5) & 0x3F, value & 0x1F
            row += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
        rows.append(bytes(row))
    return rows


def candidates(byte_length, offset=0):
    """Every plausible (width, height) for a raw 16-bit image of this size.

    Ordered by how screen-shaped the result is: real panels are wider than tall and their
    widths are multiples of 8, so those come first and are the ones worth opening.
    """
    pixels = (byte_length - offset) // 2
    if pixels <= 0:
        return []
    found = []
    for width in range(MIN_WIDTH, min(MAX_WIDTH, pixels) + 1):
        if pixels % width:
            continue
        height = pixels // width
        if not (MIN_HEIGHT <= height <= MAX_HEIGHT):
            continue
        aspect = width / height
        if not (0.5 <= aspect <= 8.0):
            continue
        # Lower sorts first: aligned widths preferred, then aspect near a typical panel.
        score = (0 if width % 8 == 0 else 1, abs(aspect - 2.0))
        found.append((score, width, height))
    found.sort()
    return [(w, h) for _, w, h in found]


def summarise_journal(journal_path):
    if not os.path.exists(journal_path):
        print(f"no journal at {journal_path}")
        return
    by_method = collections.Counter()
    errors = []
    total = 0
    for line in open(journal_path):
        entry = json.loads(line)
        total += 1
        method = entry.get("method") or "<undecoded>"
        by_method[(entry.get("dir", "?"), method)] += 1
        if isinstance(method, str) and "ERROR" in method:
            errors.append(entry)

    print(f"{total} frames\n")
    print(f"{'count':>6}  {'direction':<16} method")
    print(f"{'-' * 6}  {'-' * 16} {'-' * 48}")
    for (direction, method), count in by_method.most_common():
        arrow = "client->svc" if direction.startswith("client") else "svc->client"
        print(f"{count:>6}  {arrow:<16} {method}")

    if errors:
        print(f"\n{len(errors)} error replies, these name what the service rejected:")
        for entry in errors[:10]:
            print(f"  {entry['method']}")


def analyse_blobs(capture_dir, prefix=None, offset=0):
    blob_dir = os.path.join(capture_dir, "blobs")
    paths = sorted(glob.glob(os.path.join(blob_dir, "*.bin")))
    if prefix:
        paths = [p for p in paths if os.path.basename(p).startswith(prefix)]
    if not paths:
        print(f"\nno blobs in {blob_dir}")
        return

    out_dir = os.path.join(capture_dir, "images")
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n{len(paths)} blob(s), decoding as RGB565 into {out_dir}/\n")

    for path in paths:
        payload = open(path, "rb").read()
        digest = os.path.basename(path)[:-4]
        options = candidates(len(payload), offset)
        print(f"{digest[:12]}  {len(payload)} bytes  ({(len(payload) - offset) // 2} pixels)")
        if not options:
            print("    no plausible dimensions, not a raw image at this offset "
                  "(try --offset if it carries a header)")
            continue
        for width, height in options[:MAX_CANDIDATES]:
            rows = rgb565_rows(payload[offset:], width, height)
            name = f"{digest[:12]}_{width}x{height}.png"
            write_png(os.path.join(out_dir, name), width, height, rows)
            print(f"    {width}x{height}  -> {name}")
        if len(options) > MAX_CANDIDATES:
            print(f"    ({len(options) - MAX_CANDIDATES} further factorisations not written)")


def selftest():
    """Round-trip a known image: encode it as RGB565, recover its size, decode it back."""
    width, height = 480, 272
    pixels = []
    for y in range(height):
        for x in range(width):
            r, g, b = (x * 31) // (width - 1), (y * 63) // (height - 1), 31 - (x * 31) // (width - 1)
            pixels.append((r << 11) | (g << 5) | b)
    payload = b"".join(struct.pack("<H", p) for p in pixels)

    options = candidates(len(payload))
    assert (width, height) in options, f"the true size was not among the candidates: {options[:8]}"

    rows = rgb565_rows(payload, width, height)
    assert len(rows) == height and len(rows[0]) == width * 3, "decoded rows are the wrong shape"
    # Corners, checked against what RGB565 can actually represent rather than the input:
    # the channels are 5/6/5 bits, so 0 and full-scale are the values that survive exactly.
    assert rows[0][:3] == bytes((0, 0, 255)), f"top-left wrong: {rows[0][:3]!r}"
    assert rows[-1][-3:] == bytes((255, 255, 0)), f"bottom-right wrong: {rows[-1][-3:]!r}"

    with tempfile.TemporaryDirectory() as workspace:
        blob_dir = os.path.join(workspace, "blobs")
        os.makedirs(blob_dir)
        open(os.path.join(blob_dir, "a" * 40 + ".bin"), "wb").write(payload)
        analyse_blobs(workspace)
        written = os.path.join(workspace, "images", f"{'a' * 12}_{width}x{height}.png")
        assert os.path.exists(written), "no PNG was written for the true dimensions"
        header = open(written, "rb").read(24)
        assert header[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        assert struct.unpack(">II", header[16:24]) == (width, height), "PNG header has the wrong size"

    print("\nselftest passed: RGB565 round-trips with full-scale corners intact, the true "
          "dimensions are recovered from the byte count alone, and the PNG written for them "
          "has a valid header")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", nargs="?", help="a directory written by odr_capture.py")
    parser.add_argument("--blob", help="only analyse blobs whose SHA-1 starts with this")
    parser.add_argument("--offset", type=int, default=0, help="skip this many header bytes")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        selftest()
        return 0
    if not args.capture:
        parser.error("a capture directory is required (or --selftest)")
    summarise_journal(os.path.join(args.capture, "journal.jsonl"))
    analyse_blobs(args.capture, args.blob, args.offset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
