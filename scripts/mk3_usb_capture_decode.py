#!/usr/bin/env python3
"""Decode framed MK3 MessagePack traffic from Linux usbmon PCAPNG captures.

The tool is deliberately offline: it only reads a capture file. It understands the
64-byte usbmon header, selects control-interface traffic, splits multiple NI frames in
one USB transfer, and decodes each length-prefixed MessagePack RPC message.
Binary values are summarized by length and SHA-256 unless --include-binary is requested.
"""

import argparse
import hashlib
import json
import os
import struct
import sys

import msgpack


LINKTYPE_USB_LINUX_MMAPPED = 220
USBMON_HEADER_SIZE = 64
TRANSFER_BULK = 3

RPC_METHODS = {
    0x40: "HANDSHAKE",
    0x41: "BROWSER",
    0x42: "BROWSER_FILTER",
    0x45: "BROWSER_FLAGS",
    0x49: "PLUGIN_CHAIN",
    0x4B: "PARAMETER_PAGE",
    0x4F: "TRANSPORT",
    0x50: "INIT",
    0x51: "PARAMETER_VALUES",
    0x54: "DISPLAY",
    0x56: "GOODBYE",
    0x57: "STATE",
    0x58: "LIGHTS",
    0x59: "SYNC",
    0x5B: "UNKNOWN_5B",
}


class CaptureError(RuntimeError):
    pass


def _u32(data, offset, endian):
    return struct.unpack_from(endian + "I", data, offset)[0]


def iter_pcapng_packets(path):
    """Yield packet dictionaries from Enhanced Packet Blocks in a PCAPNG file."""
    with open(path, "rb") as stream:
        data = stream.read()

    offset = 0
    endian = "<"
    interfaces = []
    while offset < len(data):
        if len(data) - offset < 12:
            raise CaptureError(f"truncated PCAPNG block header at offset {offset}")

        if data[offset:offset + 4] == b"\x0a\x0d\x0d\x0a":
            magic = data[offset + 8:offset + 12]
            if magic == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif magic == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                raise CaptureError(f"invalid PCAPNG byte-order magic at offset {offset}")
            interfaces = []

        block_type = _u32(data, offset, endian)
        block_length = _u32(data, offset + 4, endian)
        if block_length < 12 or block_length % 4 or offset + block_length > len(data):
            raise CaptureError(f"invalid PCAPNG block length {block_length} at offset {offset}")
        if _u32(data, offset + block_length - 4, endian) != block_length:
            raise CaptureError(f"PCAPNG block length trailer mismatch at offset {offset}")

        if block_type == 1:  # Interface Description Block
            if block_length < 20:
                raise CaptureError(f"short Interface Description Block at offset {offset}")
            interfaces.append({"linktype": struct.unpack_from(endian + "H", data, offset + 8)[0]})
        elif block_type == 6:  # Enhanced Packet Block
            if block_length < 32:
                raise CaptureError(f"short Enhanced Packet Block at offset {offset}")
            interface_id, timestamp_high, timestamp_low, captured_length, packet_length = struct.unpack_from(
                endian + "IIIII", data, offset + 8)
            packet_start = offset + 28
            packet_end = packet_start + captured_length
            if interface_id >= len(interfaces):
                raise CaptureError(f"packet refers to missing interface {interface_id}")
            if packet_end > offset + block_length - 4:
                raise CaptureError(f"truncated packet data at offset {offset}")
            yield {
                "interface_id": interface_id,
                "linktype": interfaces[interface_id]["linktype"],
                "timestamp_ticks": (timestamp_high << 32) | timestamp_low,
                "captured_length": captured_length,
                "packet_length": packet_length,
                "data": data[packet_start:packet_end],
            }
        offset += block_length


def iter_usbmon_transfers(path):
    """Yield data-bearing bulk transfers from a Linux usbmon PCAPNG."""
    for packet_number, packet in enumerate(iter_pcapng_packets(path), 1):
        if packet["linktype"] != LINKTYPE_USB_LINUX_MMAPPED:
            continue
        data = packet["data"]
        if len(data) < USBMON_HEADER_SIZE:
            continue
        event_type = chr(data[8])
        transfer_type = data[9]
        endpoint = data[10]
        device = data[11]
        bus = struct.unpack_from("<H", data, 12)[0]
        requested_length = struct.unpack_from("<I", data, 32)[0]
        captured_length = struct.unpack_from("<I", data, 36)[0]
        payload = data[USBMON_HEADER_SIZE:USBMON_HEADER_SIZE + captured_length]
        if transfer_type != TRANSFER_BULK or not payload:
            continue
        yield {
            "packet_number": packet_number,
            "timestamp_ticks": packet["timestamp_ticks"],
            "event_type": event_type,
            "endpoint": endpoint,
            "device": device,
            "bus": bus,
            "direction": "device_to_host" if endpoint & 0x80 else "host_to_device",
            "requested_length": requested_length,
            "captured_length": captured_length,
            "payload": payload,
        }


def decode_rpc_message(body):
    """Decode one framed MessagePack-RPC message and derive a stable label."""
    message = msgpack.unpackb(body, raw=False, strict_map_key=False)
    if not isinstance(message, list) or len(message) < 3:
        raise ValueError(f"expected MessagePack-RPC array, got {type(message).__name__}")

    rpc_type = message[0]
    if rpc_type == 2 and len(message) == 3:
        method = message[1]
        params = message[2]
    elif rpc_type == 0 and len(message) == 4:
        method = message[2]
        params = message[3]
    elif rpc_type == 1 and len(message) == 4:
        method = None
        params = message[3]
    else:
        raise ValueError(f"unsupported MessagePack-RPC shape: {message[:2]!r}")

    if isinstance(method, int):
        label = RPC_METHODS.get(method, f"METHOD_{method}")
    elif isinstance(method, str):
        label = method.upper()
    elif method is None:
        label = "RESPONSE"
    else:
        label = "UNKNOWN"
    return message, rpc_type, method, params, label


def decode_transfer_frames(transfer):
    """Split and decode every NI frame contained in one USB transfer."""
    payload = transfer["payload"]
    offset = 0
    frames = []
    while offset < len(payload):
        remaining = len(payload) - offset
        if remaining < 4:
            raise CaptureError(
                f"USB packet {transfer['packet_number']} has {remaining} trailing byte(s) after framed data")
        body_length = struct.unpack_from("<I", payload, offset)[0]
        if body_length < 1:
            raise CaptureError(
                f"USB packet {transfer['packet_number']} has invalid NI frame length {body_length} at {offset}")
        frame_end = offset + 4 + body_length
        if frame_end > len(payload):
            raise CaptureError(
                f"USB packet {transfer['packet_number']} has truncated NI frame at {offset}: "
                f"needs {body_length}, has {len(payload) - offset - 4}")
        packed = payload[offset + 4:frame_end]
        try:
            message, rpc_type, method, params, label = decode_rpc_message(packed)
            error = None
        except (ValueError, msgpack.ExtraData, msgpack.FormatError, msgpack.StackError) as exc:
            message = None
            rpc_type = None
            method = None
            params = None
            label = "UNKNOWN"
            error = str(exc)
        frames.append({
            "transfer_offset": offset,
            "wire_length": 4 + body_length,
            "body_length": body_length,
            # Compatibility fields: this prefix was previously mistaken for a
            # separate message type, but is encoded RPC data (for example,
            # 93025492 starts [notification, method 84, params-array]).
            "message_type_hex": packed[:4].hex(),
            "message_type": label,
            "rpc_type": rpc_type,
            "rpc_method": method,
            "rpc_message": message,
            "objects": params,
            "decode_error": error,
            "packed_payload": packed,
        })
        offset = frame_end
    return frames


def iter_decoded_frames(path, endpoint=None):
    registry = None
    for transfer in iter_usbmon_transfers(path):
        if endpoint is not None and transfer["endpoint"] != endpoint:
            continue
        for frame_index, frame in enumerate(decode_transfer_frames(transfer)):
            if frame["rpc_method"] == "handshake" and isinstance(frame["objects"], list):
                for item in frame["objects"]:
                    if isinstance(item, dict) and isinstance(item.get("symbol_registry"), list):
                        registry = item["symbol_registry"]
                        break
            method = frame["rpc_method"]
            if isinstance(method, int) and registry is not None and 0 <= method < len(registry):
                frame["rpc_method_name"] = registry[method]
                frame["message_type"] = str(registry[method]).upper()
            elif isinstance(method, str):
                frame["rpc_method_name"] = method
            else:
                frame["rpc_method_name"] = None
            result = {key: value for key, value in transfer.items() if key != "payload"}
            result.update(frame)
            result["frame_index_in_transfer"] = frame_index
            yield result


def json_safe(value, include_binary=False):
    if isinstance(value, bytes):
        if include_binary:
            return {"$binary_hex": value.hex(), "length": len(value)}
        return {"$binary": {"length": len(value), "sha256": hashlib.sha256(value).hexdigest()}}
    if isinstance(value, list):
        return [json_safe(item, include_binary) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(child, include_binary) for key, child in value.items()}
    return value


def frame_record(frame, include_binary=False, include_packed=False):
    omitted = {"packed_payload"}
    record = {key: json_safe(value, include_binary) for key, value in frame.items() if key not in omitted}
    if include_packed:
        record["packed_payload_hex"] = frame["packed_payload"].hex()
    return record


def parse_endpoint(value):
    try:
        endpoint = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("endpoint must be an integer such as 4 or 0x83") from exc
    if not 0 <= endpoint <= 0xff:
        raise argparse.ArgumentTypeError("endpoint must fit in one byte")
    return endpoint


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", help="usbmon PCAPNG capture")
    parser.add_argument("--type", action="append", dest="types",
                        help="message name or 8-digit hex; repeat to select several")
    parser.add_argument("--endpoint", type=parse_endpoint, default=4,
                        help="USB endpoint address (default: host-to-device endpoint 4)")
    parser.add_argument("--jsonl", action="store_true", help="emit one JSON object per decoded frame")
    parser.add_argument("--include-binary", action="store_true", help="include complete binary values as hex")
    parser.add_argument("--include-packed", action="store_true", help="include raw MessagePack payload as hex")
    return parser.parse_args(argv)


def selected(frame, filters):
    if not filters:
        return True
    wanted = {value.upper() for value in filters}
    return frame["message_type"].upper() in wanted or frame["message_type_hex"].upper() in wanted


def main(argv=None):
    args = parse_args(argv)
    count = 0
    type_counts = {}
    try:
        frames = iter_decoded_frames(args.capture, args.endpoint)
        for frame in frames:
            if not selected(frame, args.types):
                continue
            count += 1
            type_counts[frame["message_type"]] = type_counts.get(frame["message_type"], 0) + 1
            record = frame_record(frame, args.include_binary, args.include_packed)
            if args.jsonl:
                print(json.dumps(record, separators=(",", ":"), sort_keys=True))
            else:
                print(f"packet={frame['packet_number']} offset={frame['transfer_offset']} "
                      f"{frame['direction']} ep=0x{frame['endpoint']:02x} "
                      f"method={frame['rpc_method_name'] or frame['rpc_method']} "
                      f"prefix={frame['message_type_hex']} "
                      f"wire_bytes={frame['wire_length']}")
                print(json.dumps(record["objects"], indent=2, sort_keys=True))
                if frame["decode_error"]:
                    print(f"decode_error={frame['decode_error']}")
    except (OSError, CaptureError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.jsonl:
        print(f"decoded_frames={count} capture={os.path.abspath(args.capture)} "
              f"type_counts={json.dumps(type_counts, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
