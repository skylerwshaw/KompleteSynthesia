"""LLDB breakpoint callback for passive capture of HCS bulk writes.

Import this module from LLDB, then attach its ``capture_write`` callback to
``usb_odr_interface::bulk_write_helper::write_to_bulkpipe``. On arm64 that function's
verified ABI is x2=endpoint, x3=span pointer, x4=span length. Captures are written as a
standard usbmon PCAPNG so ``mk3_usb_capture_decode.py`` can read them directly.
"""

import os
import struct
import threading
import time

try:
    import lldb
except ImportError:  # Writer helpers remain unit-testable outside LLDB.
    lldb = None


LINKTYPE_USB_LINUX_MMAPPED = 220
MAX_WRITE_BYTES = 16 * 1024 * 1024
CAPTURE_ENV = "KOMPLETE_SYNTHESIA_MK3_USB_CAPTURE"
_lock = threading.Lock()


def _block(kind, body):
    padded = body + bytes((-len(body)) % 4)
    length = 12 + len(padded)
    return struct.pack("<II", kind, length) + padded + struct.pack("<I", length)


def pcapng_header():
    section = _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    interface = _block(1, struct.pack("<HHI", LINKTYPE_USB_LINUX_MMAPPED, 0, 16 * 1024 * 1024))
    return section + interface


def usbmon_packet(payload, endpoint, timestamp_us):
    header = bytearray(64)
    struct.pack_into("<Q", header, 0, timestamp_us)
    header[8] = ord("S")
    header[9] = 3  # bulk
    header[10] = endpoint & 0xff
    header[11] = 0
    struct.pack_into("<H", header, 12, 0)
    struct.pack_into("<q", header, 16, timestamp_us // 1_000_000)
    struct.pack_into("<i", header, 24, timestamp_us % 1_000_000)
    struct.pack_into("<I", header, 32, len(payload))
    struct.pack_into("<I", header, 36, len(payload))
    return bytes(header) + payload


def append_transfer(path, endpoint, payload, timestamp_us=None):
    if timestamp_us is None:
        timestamp_us = time.time_ns() // 1000
    packet = usbmon_packet(payload, endpoint, timestamp_us)
    timestamp_high = timestamp_us >> 32
    timestamp_low = timestamp_us & 0xffffffff
    enhanced = _block(6, struct.pack("<IIIII", 0, timestamp_high, timestamp_low,
                                     len(packet), len(packet)) + packet)
    with _lock:
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "ab") as stream:
            if new_file:
                stream.write(pcapng_header())
            stream.write(enhanced)
            stream.flush()


def _log_error(path, message):
    with open(path + ".errors.log", "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def capture_write(frame, _breakpoint_location, _internal_dict):
    """LLDB callback. Return False so the service resumes immediately."""
    path = os.environ.get(CAPTURE_ENV)
    if not path:
        return False
    try:
        endpoint = frame.FindRegister("x2").GetValueAsUnsigned() & 0xff
        address = frame.FindRegister("x3").GetValueAsUnsigned()
        length = frame.FindRegister("x4").GetValueAsUnsigned()
        if not 0 < length <= MAX_WRITE_BYTES:
            _log_error(path, f"refused implausible write length {length} on endpoint {endpoint}")
            return False
        error = lldb.SBError()
        payload = frame.GetThread().GetProcess().ReadMemory(address, length, error)
        if not error.Success() or len(payload) != length:
            _log_error(path, f"ReadMemory failed for {length} bytes at 0x{address:x}: {error}")
            return False
        append_transfer(path, endpoint, payload)
    except Exception as exc:  # Never stop the service because capture failed.
        _log_error(path, f"capture callback exception: {type(exc).__name__}: {exc}")
    return False
