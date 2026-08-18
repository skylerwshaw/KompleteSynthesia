#!/usr/bin/env python3
"""Put an arbitrary image on the MK3 screen through NI's connection service.

This is the display counterpart to `odr_lightguide.py`: where that lights the keys, this
draws a client-supplied bitmap on the panel, over the same msgpack-RPC socket, without
touching USB or the legacy paths. Proven on an S61 MK3 (issue #18): the image shows up as
the header band of the parameter page.

How it works, all learned by capturing a real Komplete Kontrol session (see
MK3_DISPLAY_CAPTURE.md):

  1. The screen is not a framebuffer. The client hands the service *images as assets* and
     *models that reference them*. `add_asset` stores one PNG or WebP, addressed by the
     SHA-256 of its bytes; the store is content-addressed and survives disconnects.
  2. `client_parameter_page_set_data` carries a `plugin_data.background` field holding an
     asset handle. The device renders that image full-width behind the parameter widgets.
  3. So: upload the image, then send a page-data frame whose background is its handle.

The parameter-page frame is large and the service's argument parser aborts the whole
process on a shape it dislikes (issue #18: an unhandled msgpack exception on the strong
`sha256` type), so this does not synthesise that frame from scratch. It replays a frame
captured from real Komplete Kontrol byte for byte, substituting only two equal-length
spans: the instance UUID and the 32-byte background handle. Everything else, including the
preset's own parameter list, is left exactly as captured, which is why parameter labels
from that capture still show beneath the image.

    ./scripts/odr_display.py path/to/image.png
    ./scripts/odr_display.py path/to/image.png --hold 30
    ./scripts/odr_display.py --selftest

The image should be 1280x212 to fill the band without scaling; other sizes are accepted and
scaled by the device. The display lasts only while this holds focus; on exit the screen
returns to normal.
"""

import argparse
import hashlib
import json
import os
import socket
import struct
import sys
import time
import uuid as uuidlib

import msgpack

SOCKET_PATH = "/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks"

# A parameter-page frame captured from Komplete Kontrol, with the two spans this replaces.
# Ship a real capture under captures/ and point at it, or drop a trimmed copy in the repo.
DEFAULT_TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "captures", "03-browse")
CAPTURED_UUID = b"180a059a-3de7-4df5-a98a-966784b2e644"
CAPTURED_HANDLE = bytes.fromhex("fb4ab47505532954f1501fa8e70d43b9e804cdf9964aac5a351b69cd1df5af9f")


def frame(obj):
    body = msgpack.packb(obj, use_bin_type=True)
    return struct.pack("<I", len(body)) + body


def load_template(capture_dir):
    """The exact wire bytes of one captured client_parameter_page_set_data frame.

    Read from the raw stream, not re-encoded from the journal, so the replay is byte-for-byte
    what Komplete Kontrol sent, which is what keeps the fragile parser happy.
    """
    journal = os.path.join(capture_dir, "journal.jsonl")
    raw = open(os.path.join(capture_dir, "raw", "conn01-c2s.bin"), "rb").read()
    for line in open(journal):
        entry = json.loads(line)
        if entry.get("method") == "notify client_parameter_page_set_data":
            wire = raw[entry["raw_offset"]:entry["raw_offset"] + entry["raw_bytes"]]
            if CAPTURED_UUID in wire and CAPTURED_HANDLE in wire:
                return wire
    raise RuntimeError("no usable parameter-page frame in the capture "
                       "(need one containing the known uuid and background handle)")


def patched_frame(template, instance_uuid, handle):
    """Swap the instance UUID and background handle, both fixed-length, nothing else."""
    uuid_bytes = str(instance_uuid).encode()
    if len(uuid_bytes) != len(CAPTURED_UUID):
        raise ValueError("uuid length changed, cannot substitute in place")
    if len(handle) != len(CAPTURED_HANDLE):
        raise ValueError("handle must be 32 bytes")
    out = template.replace(CAPTURED_UUID, uuid_bytes).replace(CAPTURED_HANDLE, handle)
    if len(out) != len(template):
        raise RuntimeError("substitution changed the frame length")
    return out


class Display:
    def __init__(self, path=SOCKET_PATH):
        self.ident = uuidlib.uuid4()
        self.buf = bytearray()
        self.msgid = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(4.0)
        self.sock.connect(path)
        self.sock.sendall(self.ident.bytes)
        reply = self.request("instance_hello", [str(self.ident),
            {"client_info": {"name": "KompleteSynthesia", "version": "1.0.0", "type": "standalone"},
             "ipc_protocol_version": "2.2.0"}])
        self.registry = reply[3]["symbol_registry"]
        self.serial = reply[3]["available_devices"][0]["serialnumber"]

    def i(self, name):
        return self.registry.index(name)

    def recv(self, timeout=2.0):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.buf) >= 4:
                n = struct.unpack("<I", self.buf[:4])[0]
                if len(self.buf) >= 4 + n:
                    body = bytes(self.buf[4:4 + n]); del self.buf[:4 + n]
                    return msgpack.unpackb(body, raw=False, strict_map_key=False)
            try:
                chunk = self.sock.recv(1 << 20)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk
        return None

    def request(self, method, params):
        self.msgid += 1
        self.sock.sendall(frame([0, self.msgid, method, params]))
        return self.recv(3.0)

    def notify(self, method, params):
        self.sock.sendall(frame([2, method, params]))

    def attach(self):
        self.request(self.i("connect_device"), [str(self.ident), self.serial])
        self.notify(self.i("client_request_focus"), [str(self.ident), self.serial])
        self.notify(self.i("client_set_page"), [str(self.ident), self.serial, self.i("parameter")])
        time.sleep(0.2)

    def upload(self, image_bytes):
        handle = hashlib.sha256(image_bytes).digest()
        self.notify(self.i("add_asset"), [handle, image_bytes])
        return handle

    def show(self, template, handle):
        self.sock.sendall(patched_frame(template, self.ident, handle))

    def refocus(self):
        self.notify(self.i("client_request_focus"), [str(self.ident), self.serial])

    def close(self):
        self.sock.close()


def run(image_path, capture_dir, hold):
    image = open(image_path, "rb").read()
    kind = "PNG" if image[:8] == b"\x89PNG\r\n\x1a\n" else \
           "WebP" if image[:4] == b"RIFF" and image[8:12] == b"WEBP" else "?"
    template = load_template(capture_dir)

    display = Display()
    print(f"device {display.serial}, sending {len(image)}-byte {kind}")
    display.attach()
    handle = display.upload(image)
    time.sleep(0.4)
    display.show(template, handle)
    print(f"displayed (handle {handle.hex()[:12]}), holding {hold}s")
    end = time.time() + hold
    while time.time() < end:
        time.sleep(3)
        display.refocus()
    display.close()
    print("done, screen returns to normal")


def selftest():
    """Check the substitution without hardware: build a fake captured frame, patch it, and
    confirm both spans changed, the length held, and no stray copy of either original left."""
    inner = msgpack.packb([2, 409, [CAPTURED_UUID.decode(), "SER",
                                     {"bg": CAPTURED_HANDLE}]], use_bin_type=True)
    template = struct.pack("<I", len(inner)) + inner
    ident = uuidlib.uuid4()
    handle = hashlib.sha256(b"x").digest()
    out = patched_frame(template, ident, handle)
    assert len(out) == len(template), "length changed"
    assert str(ident).encode() in out and handle in out, "substitution did not take"
    assert CAPTURED_UUID not in out and CAPTURED_HANDLE not in out, "an original span survived"
    # And the result still decodes as the same msgpack shape.
    decoded = msgpack.unpackb(out[4:], raw=False, strict_map_key=False)
    assert decoded[0] == 2 and decoded[2][0] == str(ident), "patched frame no longer decodes"
    assert decoded[2][2]["bg"] == handle, "handle not in place after decode"
    print("selftest passed: uuid and 32-byte handle substituted in place, length preserved, "
          "no original span left behind, frame still decodes to the same shape")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", nargs="?", help="PNG or WebP to display (1280x212 fills the band)")
    parser.add_argument("--capture", default=DEFAULT_TEMPLATE,
                        help="capture dir holding a parameter-page frame to replay")
    parser.add_argument("--hold", type=float, default=20.0, help="seconds to keep it on screen")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest(); return 0
    if not args.image:
        parser.error("an image path is required (or --selftest)")
    run(args.image, args.capture, args.hold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
