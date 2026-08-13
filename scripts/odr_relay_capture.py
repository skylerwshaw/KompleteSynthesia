#!/usr/bin/env python3
"""Safe MITM relay and structured logger for the MK3 ODR msgpack-RPC socket.

Examples:
    ./scripts/odr_relay_capture.py --output /tmp/odr.jsonl --blob-dir /tmp/odr-blobs
    ./scripts/odr_relay_capture.py --marker "loaded instrument A"
    ./scripts/odr_relay_capture.py --restore

The relay moves the service socket aside only for the lifetime of the process and
restores it on every normal exit. ``--restore`` repairs a stale relay left by an
uncatchable crash, but refuses to interfere with a relay whose PID is still alive.
Captured UUIDs and device serials are redacted by default. Binary values are logged by
length and SHA-256 and, when --blob-dir is supplied, written once under that digest.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
import uuid as uuidlib

import msgpack

SOCKET_PATH = "/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks"
REAL_PATH = SOCKET_PATH + ".real"
PID_PATH = SOCKET_PATH + ".relay.pid"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class Capture:
    def __init__(self, output=None, blob_dir=None, redact=True, stream=None):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.output = open(output, "a", encoding="utf-8") if output else None
        self.stream = stream or sys.stdout
        self.blob_dir = blob_dir
        self.redact = redact
        self.identifiers = {}
        if blob_dir:
            os.makedirs(blob_dir, exist_ok=True)

    def close(self):
        if self.output:
            self.output.close()

    def remember(self, value, replacement):
        if isinstance(value, str) and value:
            self.identifiers[value] = replacement

    def _clean(self, value, key=None):
        if isinstance(value, bytes):
            digest = hashlib.sha256(value).hexdigest()
            if self.blob_dir:
                path = os.path.join(self.blob_dir, digest + ".bin")
                if not os.path.exists(path):
                    with open(path, "wb") as f:
                        f.write(value)
            return {"$binary": {"length": len(value), "sha256": digest}}
        if isinstance(value, list):
            return [self._clean(v) for v in value]
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                if self.redact and str(k) in ("serialnumber", "serial") and isinstance(v, str):
                    self.remember(v, "<serial>")
                result[str(k)] = self._clean(v, str(k))
            return result
        if self.redact and isinstance(value, str):
            if UUID_RE.match(value):
                self.remember(value, "<uuid>")
            return self.identifiers.get(value, value)
        return value

    def event(self, conn, direction, kind, message=None, **extra):
        record = {
            "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
            "connection": conn,
            "direction": direction,
            "kind": kind,
        }
        if message is not None:
            record["message"] = self._clean(message)
        record.update(extra)
        with self.lock:
            if self.output:
                self.output.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
                self.output.flush()
            summary = extra.get("method_name") or extra.get("detail") or kind
            print(f"[{record['elapsed_ms']:10.3f} ms] #{conn} {direction}: {summary}", file=self.stream, flush=True)


class ConnectionState:
    def __init__(self):
        self.registry = None

    def annotate(self, obj):
        if isinstance(obj, list) and len(obj) >= 4 and obj[0] == 1 and isinstance(obj[3], dict):
            registry = obj[3].get("symbol_registry")
            if isinstance(registry, list):
                self.registry = registry
        method = None
        if isinstance(obj, list) and len(obj) >= 3 and obj[0] in (0, 2):
            method = obj[2] if obj[0] == 0 else obj[1]
        if isinstance(method, int) and self.registry and 0 <= method < len(self.registry):
            return method, self.registry[method]
        return method, method if isinstance(method, str) else None


class Direction:
    """Incrementally decode one direction without affecting byte forwarding."""

    def __init__(self, conn_id, name, expects_preamble, capture, state):
        self.conn_id = conn_id
        self.name = name
        self.buf = bytearray()
        self.needs_preamble = expects_preamble
        self.capture = capture
        self.state = state

    def feed(self, data):
        self.buf += data
        if self.needs_preamble:
            if len(self.buf) < 16:
                return
            identity = bytes(self.buf[:16])
            del self.buf[:16]
            self.needs_preamble = False
            uuid = str(uuidlib.UUID(bytes=identity))
            self.capture.remember(uuid, "<uuid>")
            self.capture.event(self.conn_id, self.name, "preamble", detail="uuid=<uuid>" if self.capture.redact else uuid)
        while len(self.buf) >= 4:
            size = struct.unpack("<I", self.buf[:4])[0]
            if len(self.buf) < 4 + size:
                break
            body = bytes(self.buf[4:4 + size])
            del self.buf[:4 + size]
            try:
                obj = msgpack.unpackb(body, raw=False, strict_map_key=False)
                method, method_name = self.state.annotate(obj)
                self.capture.event(self.conn_id, self.name, "frame", obj,
                                   frame_length=size, method=method, method_name=method_name)
            except Exception as e:
                digest = hashlib.sha256(body).hexdigest()
                self.capture.event(self.conn_id, self.name, "undecodable",
                                   detail=str(e), frame_length=size, sha256=digest,
                                   prefix_base64=base64.b64encode(body[:48]).decode("ascii"))


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


def handle_client(client_sock, conn_id, capture):
    try:
        real_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        real_sock.connect(REAL_PATH)
    except OSError as e:
        capture.event(conn_id, "relay", "error", detail=f"cannot reach service: {e}")
        client_sock.close()
        return
    state = ConnectionState()
    capture.event(conn_id, "relay", "opened")
    c2s = Direction(conn_id, "client->service", True, capture, state)
    s2c = Direction(conn_id, "service->client", False, capture, state)
    threads = [
        threading.Thread(target=pump, args=(client_sock, real_sock, c2s), daemon=True),
        threading.Thread(target=pump, args=(real_sock, client_sock, s2c), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    client_sock.close()
    real_sock.close()
    capture.event(conn_id, "relay", "closed")


def pid_is_alive(path=PID_PATH):
    try:
        with open(path, encoding="ascii") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def restore_stale(socket_path=SOCKET_PATH, real_path=REAL_PATH, pid_path=PID_PATH):
    if not os.path.exists(real_path):
        return False, "no displaced service socket exists"
    if pid_is_alive(pid_path):
        return False, "relay process is still alive; stop it with Ctrl-C"
    try:
        if os.path.lexists(socket_path):
            os.unlink(socket_path)
        os.rename(real_path, socket_path)
        try:
            os.unlink(pid_path)
        except OSError:
            pass
        return True, "restored the service socket"
    except OSError as e:
        return False, f"restore failed: {e}"


def run_relay(args):
    if os.path.exists(REAL_PATH):
        sys.exit(f"stale {REAL_PATH}; run this script with --restore")
    if not os.path.exists(SOCKET_PATH):
        sys.exit(f"no service socket at {SOCKET_PATH}; is NIHardwareConnectionService running?")
    capture = Capture(args.output, args.blob_dir, not args.no_redact)
    os.rename(SOCKET_PATH, REAL_PATH)
    with open(PID_PATH, "w", encoding="ascii") as f:
        f.write(str(os.getpid()))

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    listener = None
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(SOCKET_PATH)
        listener.listen(5)
        listener.settimeout(1.0)
        capture.event(0, "relay", "started", detail=f"{SOCKET_PATH} -> {REAL_PATH}")
        conn_id = 0
        while True:
            try:
                client_sock, _ = listener.accept()
            except socket.timeout:
                continue
            conn_id += 1
            threading.Thread(target=handle_client, args=(client_sock, conn_id, capture), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        if listener:
            listener.close()
        if os.path.lexists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        if os.path.exists(REAL_PATH):
            os.rename(REAL_PATH, SOCKET_PATH)
        try:
            os.unlink(PID_PATH)
        except OSError:
            pass
        capture.event(0, "relay", "restored", detail="original socket path restored")
        capture.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="append sanitized events as JSON Lines")
    parser.add_argument("--blob-dir", help="write binary values by SHA-256")
    parser.add_argument("--no-redact", action="store_true", help="retain UUIDs and serials")
    parser.add_argument("--restore", action="store_true", help="restore a socket left by a crashed relay")
    parser.add_argument("--marker", help="append one marker to --output without starting the relay")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.restore:
        ok, message = restore_stale()
        print(message)
        return 0 if ok else 1
    if args.marker:
        if not args.output:
            sys.exit("--marker requires --output")
        capture = Capture(args.output, args.blob_dir, not args.no_redact)
        capture.event(0, "operator", "marker", detail=args.marker)
        capture.close()
        return 0
    run_relay(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
