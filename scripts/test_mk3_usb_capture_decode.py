import os
import struct
import tempfile
import unittest

import msgpack

import mk3_usb_capture_decode as decoder
import lldb_mk3_usb_capture as lldb_capture


def ni_frame(message_type, *objects):
    # This historical "message type" is the encoded RPC prefix [2, method, params].
    packed = b"".join(msgpack.packb(value, use_bin_type=True) for value in objects)
    body = message_type + packed
    return struct.pack("<I", len(body)) + body


def usbmon_packet(payload, endpoint=4):
    header = bytearray(64)
    header[8] = ord("S")
    header[9] = 3
    header[10] = endpoint
    header[11] = 4
    struct.pack_into("<H", header, 12, 3)
    struct.pack_into("<I", header, 32, len(payload))
    struct.pack_into("<I", header, 36, len(payload))
    return bytes(header) + payload


def block(kind, body):
    padded = body + bytes((-len(body)) % 4)
    length = 12 + len(padded)
    return struct.pack("<II", kind, length) + padded + struct.pack("<I", length)


def pcapng(packet):
    section = block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    interface = block(1, struct.pack("<HHI", decoder.LINKTYPE_USB_LINUX_MMAPPED, 0, 65535))
    enhanced = block(6, struct.pack("<IIIII", 0, 0, 123, len(packet), len(packet)) + packet)
    return section + interface + enhanced


class DecoderTests(unittest.TestCase):
    def write_capture(self, data):
        temp = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(lambda: os.unlink(temp.name))
        temp.write(data)
        temp.close()
        return temp.name

    def test_decodes_multiple_frames_in_one_transfer(self):
        display = ni_frame(bytes.fromhex("93025492"), 14, {180: [], 181: 0})
        state = ni_frame(bytes.fromhex("93025792"), 14, {120: 120.0})
        path = self.write_capture(pcapng(usbmon_packet(display + state)))
        frames = list(decoder.iter_decoded_frames(path, endpoint=4))
        self.assertEqual([frame["message_type"] for frame in frames], ["DISPLAY", "STATE"])
        self.assertEqual(frames[0]["rpc_message"], [2, 0x54, [14, {180: [], 181: 0}]])
        self.assertEqual(frames[0]["rpc_method"], 0x54)
        self.assertEqual(frames[0]["objects"], [14, {180: [], 181: 0}])
        self.assertEqual(frames[1]["objects"], [14, {120: 120.0}])

    def test_binary_values_are_hashed_by_default(self):
        value = {172: bytes(range(16))}
        safe = decoder.json_safe(value)
        self.assertEqual(safe["172"]["$binary"]["length"], 16)
        self.assertEqual(len(safe["172"]["$binary"]["sha256"]), 64)
        self.assertEqual(decoder.json_safe(value, include_binary=True)["172"]["$binary_hex"],
                         bytes(range(16)).hex())

    def test_truncated_ni_frame_is_rejected(self):
        payload = struct.pack("<I", 40) + bytes.fromhex("93025492") + b"\x0e"
        path = self.write_capture(pcapng(usbmon_packet(payload)))
        transfer = next(decoder.iter_usbmon_transfers(path))
        with self.assertRaises(decoder.CaptureError):
            decoder.decode_transfer_frames(transfer)

    def test_endpoint_filter(self):
        display = ni_frame(bytes.fromhex("93025492"), 14, {180: []})
        path = self.write_capture(pcapng(usbmon_packet(display, endpoint=0x83)))
        self.assertEqual(list(decoder.iter_decoded_frames(path, endpoint=4)), [])
        self.assertEqual(len(list(decoder.iter_decoded_frames(path, endpoint=0x83))), 1)

    def test_lldb_writer_produces_decodable_pcapng(self):
        display = ni_frame(bytes.fromhex("93025492"), 14, {180: [], 181: 0})
        path = self.write_capture(b"")
        lldb_capture.append_transfer(path, 4, display, timestamp_us=123456789)
        frames = list(decoder.iter_decoded_frames(path, endpoint=4))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["message_type"], "DISPLAY")
        self.assertEqual(frames[0]["objects"], [14, {180: [], 181: 0}])

    def test_decodes_current_string_method_handshake(self):
        message = [2, "handshake", [{"agent_version": "2.1.5-14"}]]
        body = msgpack.packb(message, use_bin_type=True)
        framed = struct.pack("<I", len(body)) + body
        path = self.write_capture(pcapng(usbmon_packet(framed)))
        frame = next(decoder.iter_decoded_frames(path, endpoint=4))
        self.assertEqual(frame["message_type"], "HANDSHAKE")
        self.assertEqual(frame["rpc_method"], "handshake")
        self.assertEqual(frame["rpc_message"], message)
        self.assertEqual(frame["objects"], message[2])

    def test_handshake_registry_resolves_following_numeric_method(self):
        registry = ["zero", "assets_add", "client_instance_parameter_page_model_set_plugin_data"]
        handshake_body = msgpack.packb(
            [2, "handshake", [{"symbol_registry": registry}]], use_bin_type=True)
        method_body = msgpack.packb([2, 2, [7, {119: 28}]], use_bin_type=True)
        payload = (struct.pack("<I", len(handshake_body)) + handshake_body
                   + struct.pack("<I", len(method_body)) + method_body)
        path = self.write_capture(pcapng(usbmon_packet(payload)))
        frames = list(decoder.iter_decoded_frames(path, endpoint=4))
        self.assertEqual(frames[1]["rpc_method_name"],
                         "client_instance_parameter_page_model_set_plugin_data")
        self.assertEqual(frames[1]["message_type"],
                         "CLIENT_INSTANCE_PARAMETER_PAGE_MODEL_SET_PLUGIN_DATA")


if __name__ == "__main__":
    unittest.main()
