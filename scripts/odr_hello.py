#!/usr/bin/env python3
"""Say hello to NI's ODR agent and print what it advertises. Read-only.

The cheapest possible probe of the service: one handshake, then the connection closes. No
device is attached to, no focus is requested, no LED or display message is sent, so this is
safe to run at any time, including with Komplete Kontrol in the foreground.

Worth running before anything else because the hello reply alone answers a lot:

  - Which devices the service can see, with their product IDs. This is how a keyboard's
    product ID is confirmed without IOKit, and it works on models this app cannot yet
    recognise (see TODO.md's test protocol).
  - The agent and IPC protocol versions, which is what the method numbering hangs off
    (see ODR_PROTOCOL.md, "Method numbering").
  - The whole `symbol_registry`: every method and field name the agent knows. `--grep`
    filters it, which is how the display surface gets mapped, by asking what the service
    can be asked to do rather than guessing.

Usage:
    ./scripts/odr_hello.py
    ./scripts/odr_hello.py --grep 'display|screen|asset|page'
    ./scripts/odr_hello.py --dump-registry registry.txt
"""

import argparse
import re
import socket
import struct
import sys
import uuid as uuidlib

import msgpack

SOCKET_PATH = "/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks"
PROTOCOL_VERSION = "2.2.0"


def hello(socket_path):
    identity = uuidlib.uuid4()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect(socket_path)
    # The 16 raw UUID bytes sit ahead of and outside the framing; without them the
    # service reads everything and answers nothing at all. See ODR_PROTOCOL.md.
    sock.sendall(identity.bytes)
    body = msgpack.packb([0, 0, "instance_hello", [
        str(identity),
        {
            "client_info": {"name": "KompleteSynthesia", "version": "1.0.0", "type": "standalone"},
            "ipc_protocol_version": PROTOCOL_VERSION,
        },
    ]], use_bin_type=True)
    sock.sendall(struct.pack("<I", len(body)) + body)

    buffer = bytearray()
    try:
        while True:
            if len(buffer) >= 4:
                size = struct.unpack("<I", buffer[:4])[0]
                if len(buffer) >= 4 + size:
                    return msgpack.unpackb(bytes(buffer[4:4 + size]), raw=False, strict_map_key=False)
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buffer += chunk
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default=SOCKET_PATH)
    parser.add_argument("--grep", help="regex to filter the symbol registry, case-insensitive")
    parser.add_argument("--dump-registry", metavar="PATH", help="write the whole registry, one 'index<TAB>name' per line")
    args = parser.parse_args()

    reply = hello(args.socket)
    if not isinstance(reply, list) or len(reply) < 4 or not isinstance(reply[3], dict):
        sys.exit(f"unexpected hello reply: {reply!r}")
    result = reply[3]

    print(f"agent          {result.get('agent_version', '?')}")
    print(f"ipc protocol   {result.get('ipc_protocol_version', '?')}")
    print(f"reply keys     {sorted(result.keys())}")

    devices = result.get("available_devices") or []
    print(f"devices        {len(devices)}")
    for device in devices:
        print(f"  {device.get('product')}  serial {device.get('serialnumber')}  "
              f"vendor {device.get('vendorID', 0):#06x}  product {device.get('productID', 0):#06x}")
    if not devices:
        print("  (none attached; everything above still works, the registry does not need hardware)")

    registry = result.get("symbol_registry")
    if not isinstance(registry, list):
        print("\nno symbol_registry in this reply: an older agent, method numbers cannot be "
              "resolved by name against it")
        return 0

    print(f"symbols        {len(registry)}")
    if args.dump_registry:
        with open(args.dump_registry, "w") as f:
            for index, name in enumerate(registry):
                f.write(f"{index}\t{name}\n")
        print(f"               written to {args.dump_registry}")
    if args.grep:
        pattern = re.compile(args.grep, re.IGNORECASE)
        matches = [(i, n) for i, n in enumerate(registry) if pattern.search(n)]
        print(f"\n{len(matches)} symbol(s) matching {args.grep!r}:")
        for index, name in matches:
            print(f"  {index:>4}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
