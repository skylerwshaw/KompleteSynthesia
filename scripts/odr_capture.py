#!/usr/bin/env python3
# Vendored from Bounga's MK3 display-capture work
# (github.com/Bounga/KompleteSynthesia, branch feat/mk3-display-capture). It produces the
# raw/ + journal.jsonl capture that the MK3 Screen template replay consumes. Capture runbook:
# docs/mk3-screen-capture.md.
"""MITM relay + structured recorder for the ODR socket, built for display traffic.

`odr_relay_capture.py` prints `repr()` of every frame, which is the right tool for the
lighting protocol: those frames are small and the interesting part is their shape. The
display side is the opposite. Its payloads are image assets, so a `repr()` dump buries the
session in megabytes of escaped bytes and scrolls the frames that matter off the screen,
and the method numbers stay raw integers that mean nothing without a registry lookup.

This records the same traffic for offline analysis instead of reading it live:

  - **Method and field numbers are resolved to names.** The hello reply carries the
    service's whole `symbol_registry`, and a number is just an index into it (see
    ODR_PROTOCOL.md). This watches the reply go past and names everything after it,
    including integer *dict keys*, which is where field names like
    `client_midi_addressing` live.
  - **Binary blobs are extracted to files**, not printed. Each is written once, keyed by
    SHA-1, and the frame keeps a reference. A 600KB image becomes one short line.
  - **Every frame is journalled to JSONL**, so `odr_analyze.py` can work over the session
    afterwards without a keyboard attached.

Bytes are relayed unmodified in both directions regardless of whether they decode; the
decoder is a passive observer, so a frame this does not understand still reaches its peer.

Usage:
    1. Quit Komplete Kontrol and any other ODR client first. An already-open connection
       bypasses the relay entirely, so clients have to reconnect through it.
    2. ./scripts/odr_capture.py --out captures/<name>
    3. Launch Komplete Kontrol and perform the action to capture.
    4. Ctrl-C to stop. The original socket path is restored automatically.

    ./scripts/odr_capture.py --selftest   # end-to-end, no hardware or NI service needed
"""

import argparse
import hashlib
import json
import os
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid as uuidlib

import msgpack

SOCKET_PATH = "/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks"

# Anything this size or larger goes to its own file rather than into the journal. Small
# enough that image assets always land in a file, large enough that serials, UUIDs and
# other short byte strings stay inline where they are readable.
BLOB_MIN_BYTES = 256

# Frames at or under this size keep their full wire bytes inline in the journal, as hex.
# Larger ones are still captured whole in the raw stream files; this bound only keeps the
# journal readable, and is set so that a protocol write-up can quote almost any control
# frame straight out of it without going to the raw stream.
HEX_INLINE_MAX = 2048

# msgpack-RPC message kinds, by their leading element.
KIND_REQUEST = 0
KIND_REPLY = 1
KIND_NOTIFY = 2


def frame(obj):
    body = msgpack.packb(obj, use_bin_type=True)
    return struct.pack("<I", len(body)) + body


class Registry:
    """The service's symbol table, learned from the hello reply rather than hardcoded.

    Empty until a hello reply goes past, so early frames are recorded with bare numbers;
    that is correct rather than a gap, since a number's meaning genuinely is unknown
    before the registry that defines it has been seen.
    """

    def __init__(self):
        self.names = []

    def learn(self, reply_result):
        if isinstance(reply_result, dict):
            registry = reply_result.get("symbol_registry")
            if isinstance(registry, list) and registry:
                self.names = registry
                return True
        return False

    def name(self, index):
        if isinstance(index, int) and 0 <= index < len(self.names):
            return self.names[index]
        return None

    def label(self, index):
        return self.name(index) or f"#{index}"


class Journal:
    """Writes one JSON line per frame, with blobs spilled to files beside it."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.blob_dir = os.path.join(out_dir, "blobs")
        os.makedirs(self.blob_dir, exist_ok=True)
        self.raw_dir = os.path.join(out_dir, "raw")
        os.makedirs(self.raw_dir, exist_ok=True)
        self.path = os.path.join(out_dir, "journal.jsonl")
        self.file = open(self.path, "a", buffering=1)
        self.lock = threading.Lock()
        self.raw_files = {}
        self.blob_count = 0
        self.frame_count = 0

    def raw_writer(self, conn_id, label):
        """An append-only verbatim copy of one direction of one connection.

        Every byte is written as it arrives, before any parsing, so the file is a faithful
        recording of the wire and stays useful even where the decoder is wrong: it is
        length-prefixed all the way down, so it re-parses standalone and can be replayed.
        The journal's `raw_offset` points into it, which is what lets a protocol write-up
        quote exact bytes for a frame rather than a re-encoding of what we understood.
        """
        key = (conn_id, label)
        if key not in self.raw_files:
            name = f"conn{conn_id:02d}-{'c2s' if label.startswith('client') else 's2c'}.bin"
            self.raw_files[key] = open(os.path.join(self.raw_dir, name), "ab", buffering=0)
        return self.raw_files[key]

    def spill(self, payload):
        digest = hashlib.sha1(payload).hexdigest()
        path = os.path.join(self.blob_dir, f"{digest}.bin")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(payload)
            self.blob_count += 1
        return {"__blob__": digest, "bytes": len(payload)}

    def encode(self, obj, registry):
        """Recursively make a decoded frame JSON-safe and readable.

        Integer dict keys are the service's field identifiers, so they get named the same
        way method numbers do. Values keep their own numbering: an integer that happens to
        be a valid registry index is far more often just a number.
        """
        if isinstance(obj, (bytes, bytearray)):
            payload = bytes(obj)
            if len(payload) >= BLOB_MIN_BYTES:
                return self.spill(payload)
            return {"__bytes__": payload.hex()}
        if isinstance(obj, dict):
            named = {}
            for key, value in obj.items():
                if isinstance(key, int):
                    label = registry.name(key)
                    named[label if label else str(key)] = self.encode(value, registry)
                else:
                    named[str(key)] = self.encode(value, registry)
            return named
        if isinstance(obj, (list, tuple)):
            return [self.encode(item, registry) for item in obj]
        return obj

    def record(self, entry):
        with self.lock:
            self.frame_count += 1
            self.file.write(json.dumps(entry, default=repr) + "\n")

    def close(self):
        for handle in self.raw_files.values():
            handle.close()
        self.file.close()


def compact(obj, depth=0):
    """A one-line rendering for the console: long lists summarised, blobs named."""
    if isinstance(obj, dict):
        if "__blob__" in obj:
            return f"<blob {obj['__blob__'][:8]} {obj['bytes']}B>"
        if "__bytes__" in obj:
            return f"0x{obj['__bytes__']}" if len(obj["__bytes__"]) <= 32 else f"<{len(obj['__bytes__']) // 2}B>"
        if depth >= 3:
            return f"{{{len(obj)} keys}}"
        return "{" + ", ".join(f"{k}: {compact(v, depth + 1)}" for k, v in list(obj.items())[:8]) + \
               (", ..." if len(obj) > 8 else "") + "}"
    if isinstance(obj, list):
        if len(obj) > 8:
            return f"[{len(obj)} items]"
        if depth >= 3:
            return f"[{len(obj)}]"
        return "[" + ", ".join(compact(item, depth + 1) for item in obj) + "]"
    if isinstance(obj, str):
        return repr(obj if len(obj) <= 60 else obj[:57] + "...")
    return repr(obj)


class Direction:
    """Decodes and records one side of one connection.

    Framing state is per direction because the client side opens with a bare 16-byte UUID
    that sits outside the length-prefixed framing; read it as a length prefix and the
    stream is misaligned for good (ODR_PROTOCOL.md says exactly this about naive relays).
    """

    def __init__(self, conn_id, label, expects_preamble, journal, registry, quiet):
        self.conn_id = conn_id
        self.label = label
        self.buf = bytearray()
        self.needs_preamble = expects_preamble
        self.journal = journal
        self.registry = registry
        self.quiet = quiet
        self.raw = journal.raw_writer(conn_id, label)
        self.consumed = 0

    def log(self, text):
        if not self.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] #{self.conn_id} {self.label}: {text}", flush=True)

    def describe(self, encoded):
        """Name the method of an already-encoded frame, for the console and the journal."""
        if not isinstance(encoded, list) or not encoded:
            return None, encoded
        kind = encoded[0]
        if kind == KIND_REQUEST and len(encoded) >= 4:
            return f"request {self.registry.label(encoded[2]) if isinstance(encoded[2], int) else encoded[2]}", encoded[3]
        if kind == KIND_NOTIFY and len(encoded) >= 3:
            return f"notify {self.registry.label(encoded[1]) if isinstance(encoded[1], int) else encoded[1]}", encoded[2]
        if kind == KIND_REPLY and len(encoded) >= 4:
            error = encoded[2]
            return f"reply{'' if error is None else ' ERROR ' + repr(error)}", encoded[3]
        return None, encoded

    def feed(self, data):
        self.raw.write(data)
        self.buf += data
        if self.needs_preamble:
            if len(self.buf) < 16:
                return
            identity = bytes(self.buf[:16])
            del self.buf[:16]
            self.needs_preamble = False
            self.journal.record({"time": time.time(), "conn": self.conn_id, "dir": self.label,
                                 "method": "preamble", "raw_offset": self.consumed,
                                 "raw_bytes": 16, "hex": identity.hex(),
                                 "frame": {"uuid": str(uuidlib.UUID(bytes=identity))}})
            self.consumed += 16
            self.log(f"preamble uuid={uuidlib.UUID(bytes=identity)}")
        while len(self.buf) >= 4:
            size = struct.unpack("<I", self.buf[:4])[0]
            if len(self.buf) < 4 + size:
                break
            wire = bytes(self.buf[:4 + size])
            del self.buf[:4 + size]
            offset = self.consumed
            self.consumed += len(wire)
            self.handle(wire[4:], wire, offset)

    def handle(self, body, wire, offset):
        provenance = {"raw_offset": offset, "raw_bytes": len(wire)}
        if len(wire) <= HEX_INLINE_MAX:
            provenance["hex"] = wire.hex()
        try:
            decoded = msgpack.unpackb(body, raw=False, strict_map_key=False)
        except Exception as e:
            self.journal.record({"time": time.time(), "conn": self.conn_id, "dir": self.label,
                                 "method": "<undecodable>", "error": str(e), **provenance})
            self.log(f"<undecodable ({e}), {len(body)} bytes>")
            return

        # The registry has to be learned before this frame is encoded, not after: the hello
        # reply that carries it is itself the first frame with named fields worth resolving.
        if isinstance(decoded, list) and len(decoded) >= 4 and decoded[0] == KIND_REPLY:
            if self.registry.learn(decoded[3]):
                self.log(f"learned symbol registry, {len(self.registry.names)} entries")

        encoded = self.journal.encode(decoded, self.registry)
        summary, params = self.describe(encoded)
        self.journal.record({"time": time.time(), "conn": self.conn_id, "dir": self.label,
                             "method": summary, **provenance, "frame": encoded})
        self.log(f"{summary or 'frame'} {compact(params)}")


def pump(src, dst, direction):
    while True:
        try:
            data = src.recv(65536)
        except OSError:
            break
        if not data:
            break
        direction.feed(data)
        try:
            dst.sendall(data)
        except OSError:
            break
    try:
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def handle_client(client_sock, conn_id, real_path, journal, quiet):
    try:
        real_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        real_sock.connect(real_path)
    except OSError as e:
        print(f"#{conn_id} could not reach the real service: {e}", flush=True)
        client_sock.close()
        return
    # One registry per connection: each client gets its own hello reply, and nothing
    # guarantees two connections are talking to the same agent build.
    registry = Registry()
    c2s = Direction(conn_id, "client->service", True, journal, registry, quiet)
    s2c = Direction(conn_id, "service->client", False, journal, registry, quiet)
    threads = [threading.Thread(target=pump, args=(client_sock, real_sock, c2s), daemon=True),
               threading.Thread(target=pump, args=(real_sock, client_sock, s2c), daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    client_sock.close()
    real_sock.close()
    if not quiet:
        print(f"#{conn_id} connection closed", flush=True)


def relay(socket_path, out_dir, quiet=False, ready=None, stop=None):
    real_path = socket_path + ".real"
    if os.path.exists(real_path):
        sys.exit(f"stale {real_path} from a previous run, remove it by hand and retry")
    if not os.path.exists(socket_path):
        sys.exit(f"no service socket at {socket_path}, is NIHardwareConnectionService running?")

    os.makedirs(out_dir, exist_ok=True)
    journal = Journal(out_dir)
    os.rename(socket_path, real_path)

    def restore(*_):
        sys.exit(0)

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, restore)
        signal.signal(signal.SIGINT, restore)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = False
    try:
        listener.bind(socket_path)
        bound = True
        listener.listen(8)
        listener.settimeout(0.5)
        print(f"relaying {socket_path} -> {real_path}", flush=True)
        print(f"recording to {journal.path}", flush=True)
        if ready is not None:
            ready.set()
        conn_id = 0
        while stop is None or not stop.is_set():
            try:
                client_sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn_id += 1
            if not quiet:
                print(f"#{conn_id} connection opened", flush=True)
            threading.Thread(target=handle_client,
                             args=(client_sock, conn_id, real_path, journal, quiet),
                             daemon=True).start()
    finally:
        listener.close()
        # Only ever remove a socket this process created. If the bind failed because the
        # agent had already recreated its own socket at the original path, unlinking here
        # would delete a live service endpoint to put our copy back over it.
        if bound:
            try:
                os.unlink(socket_path)
            except OSError:
                pass
            os.rename(real_path, socket_path)
        else:
            print(f"could not bind {socket_path}; leaving the agent's own socket alone",
                  file=sys.stderr, flush=True)
            print(f"the moved-aside original is still at {real_path}", file=sys.stderr, flush=True)
        journal.close()
        print(f"\nrestored original socket path", flush=True)
        print(f"{journal.frame_count} frames, {journal.blob_count} blobs -> {out_dir}", flush=True)


def selftest():
    """Drive the whole pipeline over real sockets against a mock service.

    Proves the three things tomorrow's capture depends on, without needing the agent or a
    keyboard: that a method number is resolved to its name through the registry, that an
    integer *field key* inside params is too, and that an image-sized payload lands in a
    blob file intact rather than in the journal.
    """
    registry_names = ["connect_device", "client_request_focus", "assets_add", "asset_image"]
    image = bytes(range(256)) * 40  # 10240 bytes, comfortably over BLOB_MIN_BYTES

    with tempfile.TemporaryDirectory() as workspace:
        socket_path = os.path.join(workspace, "agent.sock")
        out_dir = os.path.join(workspace, "capture")

        # The "real" service, which the relay will forward to once it moves this aside.
        service = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        service.bind(socket_path)
        service.listen(1)

        def serve():
            conn, _ = service.accept()
            conn.settimeout(5.0)
            conn.recv(16)
            conn.recv(65536)
            conn.sendall(frame([KIND_REPLY, 0, None, {
                "agent_version": "mock",
                "available_devices": [],
                "symbol_registry": registry_names,
            }]))
            while conn.recv(65536):
                pass
            conn.close()

        threading.Thread(target=serve, daemon=True).start()

        ready, stop = threading.Event(), threading.Event()
        relay_thread = threading.Thread(
            target=relay, args=(socket_path, out_dir), kwargs={"quiet": True, "ready": ready, "stop": stop},
            daemon=True)
        relay_thread.start()
        assert ready.wait(timeout=5.0), "relay never came up"

        identity = uuidlib.uuid4()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(socket_path)
        client.sendall(identity.bytes)
        client.sendall(frame([KIND_REQUEST, 0, "instance_hello", [str(identity), {}]]))
        assert client.recv(65536), "no hello reply came back through the relay"
        # An asset push shaped like the real thing: method by index, field key by index.
        client.sendall(frame([KIND_NOTIFY, registry_names.index("assets_add"),
                              [str(identity), {registry_names.index("asset_image"): image}]]))
        time.sleep(0.4)
        client.close()
        stop.set()
        relay_thread.join(timeout=5.0)
        service.close()

        entries = [json.loads(line) for line in open(os.path.join(out_dir, "journal.jsonl"))]
        methods = [e.get("method") for e in entries]
        assert "notify assets_add" in methods, f"method number was not resolved by name: {methods}"

        pushes = [e for e in entries if e.get("method") == "notify assets_add"]
        params = pushes[0]["frame"][2]
        assert "asset_image" in params[1], f"integer field key was not resolved: {params[1]}"
        reference = params[1]["asset_image"]
        assert reference.get("bytes") == len(image), f"blob length wrong: {reference}"

        blob_path = os.path.join(out_dir, "blobs", reference["__blob__"] + ".bin")
        assert open(blob_path, "rb").read() == image, "blob file does not match what was sent"

        # The raw stream has to be a faithful, self-parsing copy of the wire, because a
        # protocol write-up quotes bytes from it and a third party has to be able to
        # re-derive our conclusions from it without trusting our decoder.
        raw_path = os.path.join(out_dir, "raw", "conn01-c2s.bin")
        raw = open(raw_path, "rb").read()
        assert raw.startswith(identity.bytes), "raw stream does not open with the UUID preamble"
        assert image in raw, "the image bytes are missing from the raw client stream"

        # Every journalled frame must point at its own bytes in that stream.
        for entry in entries:
            if entry.get("dir", "").startswith("client") and "raw_offset" in entry:
                start = entry["raw_offset"]
                quoted = raw[start:start + entry["raw_bytes"]]
                assert len(quoted) == entry["raw_bytes"], f"raw_offset out of range: {entry['method']}"
                if "hex" in entry:
                    assert quoted.hex() == entry["hex"], f"inline hex disagrees with the raw stream: {entry['method']}"

        hello_entries = [e for e in entries if e.get("method", "").startswith("request instance_hello")]
        assert hello_entries and "hex" in hello_entries[0], "the hello frame carries no quotable hex"

        # A blob that big must not also be sitting inline in the journal.
        assert image.hex() not in open(os.path.join(out_dir, "journal.jsonl")).read(), \
            "image bytes leaked into the journal instead of being spilled to a file"

    print("selftest passed: method names and field keys resolved through the registry, "
          "image payload spilled to a blob file intact and kept out of the journal, "
          "raw wire bytes recorded verbatim with every journal entry pointing at its own "
          "bytes and inline hex agreeing with them, all end-to-end through real sockets "
          "and the real relay")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="captures/session", help="directory to record into")
    parser.add_argument("--socket", default=SOCKET_PATH, help="ODR agent socket path")
    parser.add_argument("--quiet", action="store_true", help="journal only, no console output")
    parser.add_argument("--selftest", action="store_true", help="run the offline end-to-end check")
    args = parser.parse_args()

    if args.selftest:
        selftest()
        return 0
    relay(args.socket, args.out, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
