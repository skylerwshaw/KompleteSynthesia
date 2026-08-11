#!/usr/bin/env python3
"""Light the S-series MK3 key LEDs through NI's Hardware Connection Service.

This is the reference implementation and the runnable check for the ODR IPC protocol
documented in ODR_PROTOCOL.md. It lights the keyboard without touching the legacy HID
LED mode, so the control surface keeps working, which is the whole point.

It doubles as the whole test protocol for a keyboard this project has not seen, because it
asks the service what is attached rather than consulting our own product-ID table, so it
runs on models the app itself cannot yet recognise. See TODO.md.

    ./scripts/odr_lightguide.py            # identify the keyboard, then light it
    ./scripts/odr_lightguide.py --selftest # encode-only, no hardware needed

Requires msgpack (pip install msgpack) and NIHardwareConnectionService running.
"""

import socket
import struct
import sys
import time
import uuid as uuidlib

import msgpack

SOCKET_PATH = "/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks"

METHOD_HELLO = "instance_hello"
METHOD_CONNECT_DEVICE = 382
METHOD_REQUEST_FOCUS = 373
METHOD_LIGHTGUIDE = 360
FIELD_LEDS = 239

PROTOCOL_VERSION = "2.1.0"

# Colour byte: palette index in the high six bits, intensity in the low two. Same
# encoding the legacy HID path uses, see kKompleteKontrolColor* in HIDController.h.
OFF, RED, ORANGE, YELLOW, GREEN, BLUE, PURPLE, PINK, WHITE = (
    0x00, 0x04, 0x08, 0x10, 0x1C, 0x2C, 0x34, 0x38, 0x44,
)

# The LED array is indexed by MIDI note, 0..127, not by physical key. An S88 spans
# A0..C8; smaller keyboards occupy a sub-range and ignore the rest.
LED_COUNT = 128


def frame(obj):
    body = msgpack.packb(obj, use_bin_type=True)
    return struct.pack("<I", len(body)) + body


class ODRClient:
    def __init__(self, name="KompleteSynthesia", version="1.0.0", path=SOCKET_PATH):
        identity = uuidlib.uuid4()
        self.uuid = str(identity)
        self.name = name
        self.version = version
        self.msgid = 0
        self.buffer = bytearray()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3.0)
        self.sock.connect(path)
        # The connection opens with the client UUID as 16 raw bytes, ahead of and outside
        # the length-prefixed framing. Without it the service ignores everything sent.
        self.sock.sendall(identity.bytes)

    def request(self, method, params):
        self.msgid += 1
        self.sock.sendall(frame([0, self.msgid, method, params]))
        return self.recv()

    def notify(self, method, params):
        self.sock.sendall(frame([2, method, params]))

    def recv(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.buffer) >= 4:
                size = struct.unpack("<I", self.buffer[:4])[0]
                if len(self.buffer) >= 4 + size:
                    body = bytes(self.buffer[4:4 + size])
                    del self.buffer[:4 + size]
                    return msgpack.unpackb(body, raw=False, strict_map_key=False)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buffer += chunk
        return None

    def connect_device(self):
        """Handshake, attach to the first device, take focus. Returns its serial."""
        reply = self.request(METHOD_HELLO, [
            self.uuid,
            {
                "client_info": {"name": self.name, "version": self.version, "type": "standalone"},
                "ipc_protocol_version": PROTOCOL_VERSION,
            },
        ])
        if not isinstance(reply, list) or len(reply) < 4 or not isinstance(reply[3], dict):
            raise RuntimeError(f"unexpected hello reply: {reply}")
        devices = reply[3].get("available_devices") or []
        if not devices:
            raise RuntimeError("service reports no connected devices")
        print(f"agent {reply[3].get('agent_version', '?')}")
        for d in devices:
            print(f"  {d.get('product')}  serial {d.get('serialnumber')}  "
                  f"vendor {d.get('vendorID', 0):#06x}  product {d.get('productID', 0):#06x}")
        serial = devices[0]["serialnumber"]
        reply = self.request(METHOD_CONNECT_DEVICE, [self.uuid, serial])
        # A reply is [1, msgid, error, result]; a refusal (unknown method, unknown
        # serial) carries a message in the error slot instead of leaving it None. Seen
        # in the wild as "Method not registered" when a service update renumbers this
        # method, checked rather than assumed so that doesn't look like success.
        if not isinstance(reply, list) or len(reply) < 3 or reply[2] is not None:
            raise RuntimeError(f"service refused connect_device: {reply}")
        # Lighting is ignored unless the client has asked for focus first.
        self.notify(METHOD_REQUEST_FOCUS, [self.uuid, serial])
        time.sleep(0.3)
        self.serial = serial
        return serial

    def set_leds(self, colour_for_note):
        self.notify(METHOD_LIGHTGUIDE, [
            self.uuid, self.serial,
            {FIELD_LEDS: [[n, colour_for_note(n)] for n in range(LED_COUNT)]},
        ])


def selftest():
    """Encoding check: the framed hello must match a byte capture of Komplete Kontrol's."""
    identity = uuidlib.UUID("18bc0664-8278-42dc-ba80-0f9584cb290b")
    captured = bytes.fromhex(
        "18bc0664827842dcba800f9584cb290b"          # 16-byte UUID preamble
        "96000000"                                    # uint32 LE length = 150
        "940000ae696e7374616e63655f68656c6c6f"        # [0, 0, "instance_hello", ...
        "92d92431386263303636342d383237382d343264632d626138302d306639353834636232393062"
        "82ab636c69656e745f696e666f83a46e616d65b04b6f6d706c657465204b6f6e74726f6c"
        "a776657273696f6ea5332e352e31a474797065aa7374616e64616c6f6e65"
        "b46970635f70726f746f636f6c5f76657273696f6ea5322e312e30"
    )
    built = identity.bytes + frame([0, 0, METHOD_HELLO, [
        str(identity),
        {
            "client_info": {"name": "Komplete Kontrol", "version": "3.5.1", "type": "standalone"},
            "ipc_protocol_version": PROTOCOL_VERSION,
        },
    ]])
    assert built == captured, f"hello encoding drifted:\n  {built.hex()}\n  {captured.hex()}"

    leds = [[n, BLUE] for n in range(LED_COUNT)]
    assert len(leds) == 128 and leds[-1][0] == 127, "LED array must cover MIDI 0..127"
    assert BLUE | 3 != BLUE, "intensity lives in the low two bits"
    print("selftest passed: hello matches the captured bytes, LED array well formed")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0

    client = ODRClient()
    client.connect_device()

    # Every phase addresses all 128 MIDI notes, so this makes no assumption about which
    # keyboard is attached, the device simply lights whichever of them it has.
    print("\n1. whole keyboard blue for 5s, every key should light")
    client.set_leds(lambda n: BLUE)
    time.sleep(5)

    print("2. C notes only, red, for 10s, note the lowest and highest key that lights")
    client.set_leds(lambda n: RED if n % 12 == 0 else OFF)
    time.sleep(10)

    print("3. dark")
    client.set_leds(lambda n: OFF)

    print("\nTo report a keyboard this project has not seen, send back:")
    print("  - the device line printed above, product ID especially")
    print("  - whether phase 1 lit every key")
    print("  - the lowest and highest key lit in phase 2, by name (e.g. C2 and C6)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
