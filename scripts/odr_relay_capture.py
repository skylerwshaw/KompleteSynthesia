#!/usr/bin/env python3
"""MITM relay + logger for the ODR msgpack-RPC socket.

For capturing what a real client (Komplete Kontrol) sends when the protocol drifts,
see ODR_PROTOCOL.md's "Capturing more of it" section, which this implements. Moves the
real service socket aside, binds our own listener at the original path, and forwards
every byte in both directions while decoding and printing each msgpack-RPC frame. The
original path is restored on exit (Ctrl-C, SIGTERM, or any other exit).

Usage:
    1. Quit Komplete Kontrol and any other ODR client first: an already-open
       connection bypasses the relay entirely, so it has to reconnect through it.
    2. ./scripts/odr_relay_capture.py
    3. Launch Komplete Kontrol (or any other client) and perform the action to
       capture, e.g. let it light the keyboard, browse an instrument, etc.
    4. Ctrl-C to stop; the original socket path is restored automatically.
"""

import os
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

log_lock = threading.Lock()


def log(direction, message):
    with log_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {direction}: {message}", flush=True)


class Direction:
    """Decodes one side of a connection's traffic for logging, independent of the raw
    forwarding, the bytes themselves are relayed unmodified regardless of whether
    they decode cleanly."""

    def __init__(self, name, expects_preamble):
        self.name = name
        self.buf = bytearray()
        self.needs_preamble = expects_preamble

    def feed(self, data):
        self.buf += data
        if self.needs_preamble:
            if len(self.buf) < 16:
                return
            identity = bytes(self.buf[:16])
            del self.buf[:16]
            self.needs_preamble = False
            log(self.name, f"<preamble uuid={uuidlib.UUID(bytes=identity)}>")
        while len(self.buf) >= 4:
            size = struct.unpack("<I", self.buf[:4])[0]
            if len(self.buf) < 4 + size:
                break
            body = bytes(self.buf[4:4 + size])
            del self.buf[:4 + size]
            try:
                obj = msgpack.unpackb(body, raw=False, strict_map_key=False)
            except Exception as e:
                log(self.name, f"<undecodable ({e}), {len(body)} bytes>: {body.hex()}")
                continue
            log(self.name, repr(obj))


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


def handle_client(client_sock, conn_id):
    try:
        real_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        real_sock.connect(REAL_PATH)
    except OSError as e:
        log(f"#{conn_id}", f"could not reach the real service: {e}")
        client_sock.close()
        return
    c2s = Direction(f"#{conn_id} client->service", expects_preamble=True)
    s2c = Direction(f"#{conn_id} service->client", expects_preamble=False)
    t1 = threading.Thread(target=pump, args=(client_sock, real_sock, c2s), daemon=True)
    t2 = threading.Thread(target=pump, args=(real_sock, client_sock, s2c), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    client_sock.close()
    real_sock.close()
    log(f"#{conn_id}", "connection closed")


def main():
    if os.path.exists(REAL_PATH):
        sys.exit(f"stale {REAL_PATH} from a previous run, remove it by hand and retry")
    if not os.path.exists(SOCKET_PATH):
        sys.exit(f"no service socket at {SOCKET_PATH}, is NIHardwareConnectionService running?")

    os.rename(SOCKET_PATH, REAL_PATH)

    def restore(*_):
        sys.exit(0)

    signal.signal(signal.SIGTERM, restore)
    signal.signal(signal.SIGINT, restore)

    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(SOCKET_PATH)
        listener.listen(5)
        print(f"relaying {SOCKET_PATH} -> {REAL_PATH}. Ctrl-C to stop.", flush=True)
        conn_id = 0
        listener.settimeout(1.0)
        while True:
            try:
                client_sock, _ = listener.accept()
            except socket.timeout:
                continue
            conn_id += 1
            log(f"#{conn_id}", "connection opened")
            threading.Thread(target=handle_client, args=(client_sock, conn_id), daemon=True).start()
    finally:
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        os.rename(REAL_PATH, SOCKET_PATH)
        print("restored original socket path", flush=True)


if __name__ == "__main__":
    main()
