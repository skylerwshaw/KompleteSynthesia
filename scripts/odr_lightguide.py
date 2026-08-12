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

import os
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid as uuidlib

import msgpack

SOCKET_PATH = "/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks"

METHOD_HELLO = "instance_hello"

# Everything past the hello is addressed by integer, and those numbers drift between
# agent releases; confirmed in the wild (issue #18, comment 5208665711): these three
# all came back "Method not registered" after an update, having worked before it.
# What's stable is the *name*: the hello reply carries the service's whole
# symbol_registry array, and a method's number is just its index in that array
# (confirmed against real Komplete Kontrol traffic via a relay; see ODR_PROTOCOL.md).
# So these are names, resolved fresh in connect_device() every run, not numbers that
# go stale again next update.
SYMBOL_CONNECT_DEVICE = "connect_device"
SYMBOL_REQUEST_FOCUS = "client_request_focus"
SYMBOL_LIGHTGUIDE = "client_lightguide_set_leds"
SYMBOL_MIDI_ADDRESSING = "client_midi_addressing"

# Agent 2.0.7 (R15) / IPC protocol 2.1.0 is not known to include a symbol_registry in its
# hello reply at all (nobody needed one before the numbers moved). If a reply has no
# registry, these are what that generation was confirmed using, kept as a fallback so
# an un-updated service still lights the keyboard instead of being told it can't.
LEGACY_METHOD_CONNECT_DEVICE = 382
LEGACY_METHOD_REQUEST_FOCUS = 373
LEGACY_METHOD_LIGHTGUIDE = 360
LEGACY_FIELD_MIDI_ADDRESSING = 239

PROTOCOL_VERSION = "2.1.0"


def check_reply_accepted(reply, method_name):
    """Raise if a request reply ([1, msgid, error, result]) carries a refusal.

    A service that has renumbered its methods (observed in the wild as
    "Method not registered" after a Hardware Connection Service update, see
    issue #18) refuses instead of erroring out the connection, so this has to be
    checked rather than assumed.
    """
    if not isinstance(reply, list) or len(reply) < 3 or reply[2] is not None:
        raise RuntimeError(f"service refused {method_name}: {reply}")

# Colour byte: palette index in the high six bits, intensity in the low two. Same
# encoding the legacy HID path uses, see kKompleteKontrolColor* in HIDController.h.
OFF, RED, ORANGE, YELLOW, GREEN, BLUE, PURPLE, PINK, WHITE = (
    0x00, 0x04, 0x08, 0x10, 0x1C, 0x2C, 0x34, 0x38, 0x44,
)

# The LED array is indexed by MIDI note, 0..127, not by physical key. An S88 spans
# A0..C8; smaller keyboards occupy a sub-range and ignore the rest.
LED_COUNT = 128

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n):
    """Scientific pitch notation, e.g. 60 -> 'C4' (middle C), the same convention
    TODO.md already asks testers to report in."""
    return f"{_NOTE_NAMES[n % 12]}{n // 12 - 1}"


def frame(obj):
    body = msgpack.packb(obj, use_bin_type=True)
    return struct.pack("<I", len(body)) + body


def recv_frame(conn):
    """Reads one length-prefixed frame and decodes its body. The mock servers'
    counterpart to ODRClient.recv(), needed because a bare conn.recv() includes the
    4-byte length prefix, which msgpack.unpackb chokes on ('received extra data')."""
    header = b""
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    size = struct.unpack("<I", header)[0]
    body = b""
    while len(body) < size:
        chunk = conn.recv(size - len(body))
        if not chunk:
            return None
        body += chunk
    return msgpack.unpackb(body, raw=False, strict_map_key=False)


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
        registry = reply[3].get("symbol_registry")
        if isinstance(registry, list):
            def resolve(name):
                try:
                    return registry.index(name)
                except ValueError:
                    raise RuntimeError(f"service's symbol registry has no {name!r}") from None

            connect_device_method = resolve(SYMBOL_CONNECT_DEVICE)
            request_focus_method = resolve(SYMBOL_REQUEST_FOCUS)
            self.leds_method = resolve(SYMBOL_LIGHTGUIDE)
            self.addr_field = resolve(SYMBOL_MIDI_ADDRESSING)
        else:
            # No registry at all, an older agent as far as is known (see the
            # LEGACY_* comment above). Not an error: fall back to the numbers that
            # generation was confirmed using.
            print("no symbol registry in hello reply, using legacy method numbers")
            connect_device_method = LEGACY_METHOD_CONNECT_DEVICE
            request_focus_method = LEGACY_METHOD_REQUEST_FOCUS
            self.leds_method = LEGACY_METHOD_LIGHTGUIDE
            self.addr_field = LEGACY_FIELD_MIDI_ADDRESSING

        serial = devices[0]["serialnumber"]
        reply = self.request(connect_device_method, [self.uuid, serial])
        check_reply_accepted(reply, "connect_device")
        # Lighting is ignored unless the client has asked for focus first.
        self.notify(request_focus_method, [self.uuid, serial])
        time.sleep(0.3)
        self.serial = serial
        return serial

    def set_leds(self, colour_for_note):
        self.notify(self.leds_method, [
            self.uuid, self.serial,
            {self.addr_field: [[n, colour_for_note(n)] for n in range(LED_COUNT)]},
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

    # A real refusal, captured by @Bounga against Hardware Connection Service 2.1.5
    # after it renumbered connect_device out from under this script (issue #18,
    # comment 5208665711), not a synthetic guess at the reply shape.
    refused = [1, 2, "Method not registered: {}", None]
    try:
        check_reply_accepted(refused, "connect_device")
        raise AssertionError("check_reply_accepted let a real captured refusal through")
    except RuntimeError:
        pass
    check_reply_accepted([1, 2, None, True], "connect_device")  # must not raise

    check_refusal_surfaces_through_a_real_socket()
    check_legacy_fallback_through_a_real_socket()

    print("selftest passed: hello matches the captured bytes, LED array well formed, "
          "refusal check catches Bounga's captured 2.1.5 refusal, legacy fallback works "
          "when there's no registry, all end-to-end through a real socket")


def _serve_mock_refusal(sock_path, ready):
    """One-shot fake service: a hello with one fake device, then Bounga's captured
    connect_device refusal verbatim. Enough to drive ODRClient.connect_device() through
    its real send/recv/framing code, not just the parsing function in isolation."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    ready.set()
    conn, _ = server.accept()
    conn.settimeout(3.0)
    conn.recv(16)  # the raw UUID preamble, sent ahead of the framing
    conn.recv(4096)  # the hello request; contents unused, order is all that matters here
    conn.sendall(frame([1, 1, None, {
        "available_devices": [{"product": "MOCK S88 MK3", "serialnumber": "MOCK",
                                "vendorID": 0x17cc, "productID": 0x2120}],
        "agent_version": "2.1.5",
        # Order doesn't matter for resolve() (it searches by name), only presence.
        # Unlike the real registry, this is nowhere near 437 entries, on purpose.
        "symbol_registry": [SYMBOL_CONNECT_DEVICE, SYMBOL_REQUEST_FOCUS,
                             SYMBOL_LIGHTGUIDE, SYMBOL_MIDI_ADDRESSING],
    }]))
    conn.recv(4096)  # the connect_device request
    conn.sendall(frame([1, 2, "Method not registered: {}", None]))
    conn.close()
    server.close()


def check_refusal_surfaces_through_a_real_socket():
    with tempfile.TemporaryDirectory() as d:
        sock_path = os.path.join(d, "mock.sock")
        ready = threading.Event()
        server = threading.Thread(target=_serve_mock_refusal, args=(sock_path, ready), daemon=True)
        server.start()
        ready.wait(timeout=3.0)
        try:
            ODRClient(path=sock_path).connect_device()
        except RuntimeError as e:
            assert "Method not registered" in str(e), f"wrong error surfaced: {e}"
        else:
            raise AssertionError("connect_device did not raise against a mocked refusal")
        server.join(timeout=3.0)


def _serve_mock_legacy(sock_path, ready, errors):
    """One-shot fake service shaped like agent 2.0.7 (R15): a hello reply with no
    symbol_registry at all, then accepts connect_device addressed by the legacy number
    and a lightguide notify with the legacy field key. Runs in a background thread, so
    failures are appended to `errors` rather than raised: an exception here would only
    print a traceback and be silently swallowed, not fail the test."""
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        ready.set()
        conn, _ = server.accept()
        conn.settimeout(3.0)
        conn.recv(16)
        conn.recv(4096)  # hello request
        conn.sendall(frame([1, 1, None, {
            "available_devices": [{"product": "MOCK S88 MK3", "serialnumber": "MOCK",
                                    "vendorID": 0x17cc, "productID": 0x2120}],
            "agent_version": "2.0.7",
            # deliberately no "symbol_registry" key
        }]))
        request = recv_frame(conn)  # connect_device request, must use the legacy number
        if request[2] != LEGACY_METHOD_CONNECT_DEVICE:
            errors.append("client did not use the legacy method number for connect_device")
        conn.sendall(frame([1, 2, None, True]))
        recv_frame(conn)  # focus notify
        _, method, params = recv_frame(conn)  # lightguide notify, must use the legacy field key
        if method != LEGACY_METHOD_LIGHTGUIDE or LEGACY_FIELD_MIDI_ADDRESSING not in params[2]:
            errors.append(f"client did not use legacy lightguide method/field: {method}, {params[2].keys()}")
        conn.close()
        server.close()
    except Exception as e:  # noqa: BLE001 (report, don't silently swallow)
        errors.append(f"mock service raised: {e!r}")


def check_legacy_fallback_through_a_real_socket():
    with tempfile.TemporaryDirectory() as d:
        sock_path = os.path.join(d, "mock.sock")
        ready = threading.Event()
        errors = []
        server = threading.Thread(target=_serve_mock_legacy, args=(sock_path, ready, errors), daemon=True)
        server.start()
        ready.wait(timeout=3.0)
        client = ODRClient(path=sock_path)
        client.connect_device()
        client.set_leds(lambda n: 0)
        server.join(timeout=3.0)
        assert not errors, "; ".join(errors)


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0

    client = ODRClient()
    client.connect_device()

    # Every phase addresses all 128 MIDI notes, so this makes no assumption about which
    # keyboard is attached; the device simply lights whichever of them it has. GREEN,
    # not BLUE: the S88 MK3's own default/idle lighting is already blue-ish, which made
    # phase 1 hard to distinguish from "did nothing."
    print("\n1. whole keyboard green for 5s, every key should light")
    client.set_leds(lambda n: GREEN)
    time.sleep(5)

    c_notes = [n for n in range(LED_COUNT) if n % 12 == 0]
    print(f"2. red on {', '.join(note_name(n) for n in c_notes)} for 10s: "
          f"check those specific keys light, and only those")
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
