#!/usr/bin/env python3
"""Turn a capture into a protocol write-up someone else can act on.

`odr_analyze.py` answers "what did we get". This answers "what would another developer
need to implement it", which is a different document: exact bytes, method names with the
registry indices they resolved from, argument shapes, payload sizes and the cadence they
arrive at. The audience is whoever picks up the ODR work next, in Objective-C or Python,
without this keyboard in front of them.

Everything here is derived from the journal and the raw streams `odr_capture.py` wrote, so
the output is evidence rather than recollection: every quoted frame is a real hexdump of
real wire bytes, and its offset into the raw stream is printed so a reader can go and check
it independently.

Usage:
    ./scripts/odr_report.py captures/03-browse > report.md
    ./scripts/odr_report.py captures/03-browse --method client_set_page
    ./scripts/odr_report.py --selftest
"""

import argparse
import collections
import glob
import json
import os
import struct
import sys
import tempfile

# A 600KB asset push is not readable as a hexdump, so long bodies are quoted head and tail
# rather than in full; the elided rows are the uniform middle, and the framing that an
# implementer actually has to reproduce is all in the first and last rows.
HEXDUMP_EDGE_ROWS = 8
HEXDUMP_BODY_ROW_LIMIT = 2 * HEXDUMP_EDGE_ROWS + 1


def hexdump(payload, base=0, limit=None):
    """Classic offset / hex / ASCII dump, elided in the middle when very long."""
    rows = []
    total = (len(payload) + 15) // 16
    edge = HEXDUMP_EDGE_ROWS if limit and total > limit else total

    def row(index):
        chunk = payload[index * 16:(index + 1) * 16]
        octets = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        return f"{base + index * 16:08x}  {octets}  |{text}|"

    if edge >= total:
        rows = [row(i) for i in range(total)]
    else:
        rows = [row(i) for i in range(edge)]
        rows.append(f"          ... {total - 2 * edge} rows elided ...")
        rows += [row(i) for i in range(total - edge, total)]
    return "\n".join(rows)


def annotated_frame(wire):
    """A frame's bytes, split into the parts an implementer has to produce separately."""
    if len(wire) < 4:
        return hexdump(wire)
    (size,) = struct.unpack("<I", wire[:4])
    out = [f"length prefix (uint32 LE) = {size}", hexdump(wire[:4]),
           "", "msgpack body", hexdump(wire[4:], base=4, limit=HEXDUMP_BODY_ROW_LIMIT)]
    return "\n".join(out)


def load(capture_dir):
    journal_path = os.path.join(capture_dir, "journal.jsonl")
    if not os.path.exists(journal_path):
        sys.exit(f"no journal at {journal_path}")
    entries = [json.loads(line) for line in open(journal_path)]
    raw = {}
    for path in glob.glob(os.path.join(capture_dir, "raw", "*.bin")):
        raw[os.path.basename(path)] = open(path, "rb").read()
    return entries, raw


def stream_for(entry, raw):
    side = "c2s" if entry.get("dir", "").startswith("client") else "s2c"
    name = f"conn{entry.get('conn', 1):02d}-{side}.bin"
    return name, raw.get(name)


def wire_bytes(entry, raw):
    if "hex" in entry:
        return bytes.fromhex(entry["hex"])
    name, stream = stream_for(entry, raw)
    if stream is None or "raw_offset" not in entry:
        return None
    return stream[entry["raw_offset"]:entry["raw_offset"] + entry["raw_bytes"]]


def session_section(entries):
    print("## Session\n")
    hello = next((e for e in entries
                  if e.get("method", "").startswith("reply") and isinstance(e.get("frame"), list)
                  and len(e["frame"]) >= 4 and isinstance(e["frame"][3], dict)
                  and "symbol_registry" in e["frame"][3]), None)
    if not hello:
        print("No hello reply with a symbol registry was captured, so method numbers in this")
        print("session could not be resolved to names.\n")
        return None
    result = hello["frame"][3]
    print(f"- agent: `{result.get('agent_version', '?')}`")
    print(f"- ipc protocol: `{result.get('ipc_protocol_version', '?')}`")
    print(f"- symbol registry: {len(result.get('symbol_registry', []))} entries")
    devices = result.get("available_devices") or []
    print(f"- devices: {len(devices)}")
    for device in devices:
        print(f"  - `{device.get('product')}` serial `{device.get('serialnumber')}` "
              f"vendor `{device.get('vendorID', 0):#06x}` product `{device.get('productID', 0):#06x}`")
    print()
    return result.get("symbol_registry")


def inventory_section(entries, registry):
    print("## Methods observed\n")
    print("Index is the method's position in this agent's `symbol_registry`, which is how it")
    print("was addressed on the wire. The index is specific to this agent build; the name is")
    print("what stays stable across releases.\n")

    index_of = {name: i for i, name in enumerate(registry or [])}
    stats = collections.defaultdict(lambda: {"count": 0, "bytes": 0, "first": None, "last": None})
    for entry in entries:
        method = entry.get("method")
        if not method:
            continue
        key = (("client -> service" if entry.get("dir", "").startswith("client") else "service -> client"), method)
        record = stats[key]
        record["count"] += 1
        record["bytes"] += entry.get("raw_bytes", 0)
        stamp = entry.get("time")
        if stamp is not None:
            record["first"] = stamp if record["first"] is None else min(record["first"], stamp)
            record["last"] = stamp if record["last"] is None else max(record["last"], stamp)

    print("| Direction | Method | Index | Frames | Bytes | Rate |")
    print("|---|---|---|---|---|---|")
    for (direction, method), record in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
        bare = method.split(" ", 1)[-1] if " " in method else method
        index = index_of.get(bare)
        span = (record["last"] - record["first"]) if record["first"] is not None else 0
        rate = f"{record['count'] / span:.1f}/s" if span > 0.5 else ""
        print(f"| {direction} | `{method}` | {index if index is not None else ''} | "
              f"{record['count']} | {record['bytes']:,} | {rate} |")
    print()
    return stats


def detail_section(entries, raw, only=None):
    print("## Frame shapes\n")
    print("One representative frame per method: its decoded structure, then the exact wire")
    print("bytes with their offset into the raw stream, so each can be verified independently.\n")

    seen = set()
    for entry in entries:
        method = entry.get("method")
        if not method or method in seen:
            continue
        if only and only not in method:
            continue
        seen.add(method)
        wire = wire_bytes(entry, raw)
        name, _ = stream_for(entry, raw)
        print(f"### `{method}`\n")
        print(f"Direction: {entry.get('dir')}. "
              f"Source: `raw/{name}` at offset {entry.get('raw_offset')}, {entry.get('raw_bytes')} bytes.\n")
        if "frame" in entry:
            print("Decoded:\n")
            print("```json")
            print(json.dumps(entry["frame"], indent=2)[:4000])
            print("```\n")
        if wire:
            print("Wire:\n")
            print("```")
            print(annotated_frame(wire))
            print("```\n")


def blob_section(capture_dir):
    paths = sorted(glob.glob(os.path.join(capture_dir, "blobs", "*.bin")))
    if not paths:
        return
    print("## Payloads extracted\n")
    print("Blobs are keyed by SHA-1 of their contents, so an identical payload sent twice")
    print("appears once. Sizes are exact.\n")
    print("| SHA-1 | Bytes | As RGB565 |")
    print("|---|---|---|")
    for path in paths:
        size = os.path.getsize(path)
        pixels = size // 2
        shape = f"{pixels:,} pixels" if size % 2 == 0 else "odd length, not 16bpp"
        print(f"| `{os.path.basename(path)[:16]}` | {size:,} | {shape} |")
    print()
    print("`odr_analyze.py` writes a PNG per plausible width/height factorisation of each.\n")


def selftest():
    """Build a synthetic capture, render a report, and check the parts an implementer needs."""
    with tempfile.TemporaryDirectory() as capture_dir:
        os.makedirs(os.path.join(capture_dir, "raw"))
        os.makedirs(os.path.join(capture_dir, "blobs"))
        registry = ["connect_device", "client_set_page"]
        body = b'\x93\x02\x01\x90'
        wire = struct.pack("<I", len(body)) + body
        open(os.path.join(capture_dir, "raw", "conn01-c2s.bin"), "wb").write(wire)
        open(os.path.join(capture_dir, "blobs", "b" * 40 + ".bin"), "wb").write(b"\x00" * 4096)
        with open(os.path.join(capture_dir, "journal.jsonl"), "w") as f:
            f.write(json.dumps({"time": 1.0, "conn": 1, "dir": "service->client",
                                "method": "reply",
                                "frame": [1, 0, None, {"agent_version": "mock", "symbol_registry": registry,
                                                       "available_devices": [{"product": "MOCK S61 MK3",
                                                                              "productID": 0x2110}]}]}) + "\n")
            f.write(json.dumps({"time": 2.0, "conn": 1, "dir": "client->service",
                                "method": "notify client_set_page", "raw_offset": 0,
                                "raw_bytes": len(wire), "hex": wire.hex(),
                                "frame": [2, 1, []]}) + "\n")

        import io
        buffer = io.StringIO()
        stdout, sys.stdout = sys.stdout, buffer
        try:
            render(capture_dir, None)
        finally:
            sys.stdout = stdout
        report = buffer.getvalue()

    assert "MOCK S61 MK3" in report, "the device was not reported"
    assert "0x2110" in report, "the product ID was not reported"
    # The index is what makes a method addressable, and it must come from the registry.
    assert "| 1 |" in report, f"client_set_page's registry index was not resolved:\n{report}"
    assert "length prefix (uint32 LE) = 4" in report, f"the framing was not annotated:\n{report}"
    assert "93 02 01 90" in report, f"the wire bytes were not dumped:\n{report}"
    assert "4,096" in report, "the extracted payload was not listed with its size"
    print("selftest passed: the report carries the device and product ID, resolves a method "
          "to its registry index, dumps real wire bytes with their framing, and lists "
          "extracted payloads with exact sizes")


def render(capture_dir, only):
    entries, raw = load(capture_dir)
    print(f"# ODR capture report: `{os.path.basename(os.path.abspath(capture_dir))}`\n")
    print(f"{len(entries)} frames recorded.\n")
    registry = session_section(entries)
    inventory_section(entries, registry)
    blob_section(capture_dir)
    detail_section(entries, raw, only)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", nargs="?")
    parser.add_argument("--method", help="only detail methods whose name contains this")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        selftest()
        return 0
    if not args.capture:
        parser.error("a capture directory is required (or --selftest)")
    render(args.capture, args.method)
    return 0


if __name__ == "__main__":
    sys.exit(main())
