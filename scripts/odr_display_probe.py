#!/usr/bin/env python3
"""Inspect ODR captures and run guarded MK3 display experiments.

The capture inspector is usable before the display wire shape is known. The live
subcommands deliberately refuse to guess: ``static`` and ``refresh`` consume a schema
fixture produced from a real Komplete Kontrol capture, so method IDs and nested fields
are always resolved from that service generation's symbol registry.
"""

import argparse
import base64
import collections
import copy
import hashlib
import io
import json
import os
import socket
import struct
import sys
import tempfile
import time
import uuid

import msgpack

SOCKET_PATH = "/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks"
SERVICE_LOG_PATH = os.path.expanduser(
    "~/Library/Logs/Native Instruments/NIHardwareConnectionService.log")
DISPLAY_NAMES = {
    "add_asset",
    "client_set_page",
    "client_parameter_page_set_plugin_data",
    "client_parameter_page_set_data",
    "client_plugin_chain_set_data",
}


def iter_records(path):
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"{path}:{number}: invalid JSON: {e}") from e


def inspect_capture(path, output=None):
    counts = collections.Counter()
    relevant = []
    registries = 0
    registry = None
    for record in iter_records(path):
        if record.get("kind") != "frame":
            continue
        name = record.get("method_name")
        if name:
            counts[str(name)] += 1
        message = record.get("message")
        if isinstance(message, list) and len(message) >= 4 and isinstance(message[3], dict):
            if "symbol_registry" in message[3]:
                registries += 1
                candidate = message[3]["symbol_registry"]
                if isinstance(candidate, list):
                    registry = candidate
        if name in DISPLAY_NAMES or (isinstance(name, str) and any(term in name for term in ("asset", "page", "plugin"))):
            if registry:
                record["named_message"] = name_dictionary_keys(message, registry)
            relevant.append(record)
    summary = {
        "capture": os.path.abspath(path),
        "hello_replies_with_registry": registries,
        "method_counts": dict(sorted(counts.items())),
        "display_events": relevant,
    }
    encoded = json.dumps(summary, indent=2, sort_keys=True)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(encoded + "\n")
    else:
        print(encoded)
    return summary


def name_dictionary_keys(value, registry):
    """Resolve integer map keys only; integer values may be enums or ordinary data."""
    if isinstance(value, list):
        return [name_dictionary_keys(v, registry) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                number = -1
            named = registry[number] if 0 <= number < len(registry) else str(key)
            result[named] = name_dictionary_keys(child, registry)
        return result
    return value


def frame(value):
    body = msgpack.packb(value, use_bin_type=True)
    return struct.pack("<I", len(body)) + body


def recv_frame(sock):
    header = b""
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            raise RuntimeError("service closed while reading frame header")
        header += chunk
    length = struct.unpack("<I", header)[0]
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise RuntimeError("service closed while reading frame body")
        body += chunk
    return msgpack.unpackb(body, raw=False, strict_map_key=False)


class Session:
    def __init__(self, path=SOCKET_PATH):
        self.identity = uuid.uuid4()
        self.uuid = str(self.identity)
        self.msgid = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(path)
        self.sock.sendall(self.identity.bytes)
        hello = self.request("instance_hello", [
            self.uuid,
            {"client_info": {"name": "KompleteSynthesia Display Probe", "version": "0.1", "type": "standalone"},
             "ipc_protocol_version": "2.1.0"},
        ])
        if not isinstance(hello, list) or len(hello) < 4 or not isinstance(hello[3], dict):
            raise RuntimeError(f"unexpected hello reply: {hello!r}")
        self.hello = hello[3]
        self.registry = self.hello.get("symbol_registry")
        if not isinstance(self.registry, list):
            raise RuntimeError("display probing requires a service symbol_registry")

    def close(self):
        self.sock.close()

    def resolve(self, name):
        try:
            return self.registry.index(name)
        except ValueError:
            raise RuntimeError(f"service has no symbol {name!r}") from None

    def request(self, method, params):
        self.msgid += 1
        self.sock.sendall(frame([0, self.msgid, method, params]))
        reply = recv_frame(self.sock)
        if not isinstance(reply, list) or len(reply) < 4 or reply[2] is not None:
            raise RuntimeError(f"service refused {method!r}: {reply!r}")
        return reply

    def notify(self, method, params):
        self.sock.sendall(frame([2, method, params]))


def inventory(path=SOCKET_PATH):
    session = Session(path)
    try:
        assets = session.hello.get("registered_assets") or []
        surface_terms = ("asset", "image", "display", "screen", "frame", "video")
        surface_symbols = [
            {"index": index, "name": name}
            for index, name in enumerate(session.registry)
            if any(term in str(name).lower() for term in surface_terms)
        ]
        surface_neighborhoods = {
            str(item["index"]): [
                {"index": neighbor, "name": session.registry[neighbor]}
                for neighbor in range(max(0, item["index"] - 5), min(len(session.registry), item["index"] + 6))
            ]
            for item in surface_symbols
        }
        midi_template_terms = ("midi", "template", "assignment", "configuration")
        midi_template_symbols = [
            {"index": index, "name": name}
            for index, name in enumerate(session.registry)
            if any(term in str(name).lower() for term in midi_template_terms)
        ]
        midi_template_neighborhoods = {
            str(item["index"]): [
                {"index": neighbor, "name": session.registry[neighbor]}
                for neighbor in range(max(0, item["index"] - 5), min(len(session.registry), item["index"] + 6))
            ]
            for item in midi_template_symbols
        }
        print(json.dumps({
            "agent_version": session.hello.get("agent_version"),
            "ipc_protocol_version": session.hello.get("ipc_protocol_version"),
            "available_devices": session.hello.get("available_devices") or [],
            "registered_asset_count": len(assets),
            "registered_assets": [asset.hex() if isinstance(asset, bytes) else str(asset) for asset in assets],
            "surface_symbols": surface_symbols,
            "surface_symbol_neighborhoods": surface_neighborhoods,
            "midi_template_symbols": midi_template_symbols,
            "midi_template_symbol_neighborhoods": midi_template_neighborhoods,
        }, indent=2, default=str))
    finally:
        session.close()


def wait_for_device(timeout=15.0, path=SOCKET_PATH):
    """Wait read-only until a freshly opened service session reports one device."""
    if not 0 < timeout <= 60:
        raise RuntimeError("wait-device timeout must be above zero and no higher than 60 seconds")
    deadline = time.monotonic() + timeout
    last_count = None
    last_error = None
    while time.monotonic() < deadline:
        session = None
        try:
            session = Session(path)
            devices = session.hello.get("available_devices") or []
            last_count = len(devices)
            if last_count == 1:
                serial = devices[0].get("serialnumber")
                print(f"service reports one available device: {serial}")
                return
            if last_count > 1:
                raise RuntimeError(f"expected one MK3 device, service reports {last_count}")
            last_error = None
        except (ConnectionError, OSError, socket.timeout, RuntimeError) as exc:
            last_error = str(exc)
        finally:
            if session is not None:
                session.close()
        time.sleep(0.25)
    detail = f"last device count={last_count}"
    if last_error:
        detail += f", last error={last_error}"
    raise RuntimeError(f"service did not report one MK3 device within {timeout:g}s ({detail})")


def summarize_setting_value(value, preview=96):
    """Return bounded metadata for a setting value, including opaque image data."""
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
            "prefix_hex": value[:32].hex(),
        }
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(value) <= preview:
            return value
        return {
            "type": "string",
            "characters": len(value),
            "utf8_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "prefix": value[:preview],
        }
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    if isinstance(value, dict):
        return {"type": "map", "keys": [str(key) for key in value]}
    return value


def collect_setting_fields(value, wanted, path="settings"):
    """Collect selected named fields without reproducing the full settings tree."""
    found = []
    if isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(collect_setting_fields(child, wanted, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in wanted:
                found.append({"path": child_path, "value": summarize_setting_value(child)})
            found.extend(collect_setting_fields(child, wanted, child_path))
    return found


def settings_inventory(path=SOCKET_PATH):
    """Read and summarize settings; this does not request focus or mutate the device."""
    session = Session(path)
    try:
        devices = session.hello.get("available_devices") or []
        if len(devices) != 1:
            raise RuntimeError(f"expected exactly one MK3 device, service reports {len(devices)}")
        serial = devices[0].get("serialnumber")
        if not serial:
            raise RuntimeError("connected device has no serialnumber")
        reply = request_registered(session, "get_device_settings", [session.uuid, serial])
        named = name_dictionary_keys(reply[3], session.registry)
        fields = collect_setting_fields(named, {
            "active_midi_template",
            "color",
            "hide_template_name",
            "image_data",
            "midi_output_preference",
            "pages",
            "template_name",
        })
        result = {
            "agent_version": session.hello.get("agent_version"),
            "device_model": devices[0].get("model"),
            "result_type": type(named).__name__,
            "top_level": summarize_setting_value(named),
            "selected_fields": fields,
        }
        print(json.dumps(result, indent=2, default=str))
        return result
    finally:
        session.close()


def request_registered(session, name, params):
    """Prefer registry addressing, with a literal-name fallback for mixed RPC tables."""
    try:
        method = session.resolve(name)
    except RuntimeError:
        method = name
    try:
        return session.request(method, params)
    except RuntimeError as error:
        if method == name or "Method not registered" not in str(error):
            raise
        return session.request(name, params)


def mapping_field(mapping, registry, name):
    """Read a registry-packed map field while preserving its original key form."""
    if not isinstance(mapping, dict):
        raise RuntimeError(f"expected a map containing {name!r}, got {type(mapping).__name__}")
    try:
        numeric = registry.index(name)
    except ValueError:
        numeric = None
    for key in (numeric, name):
        if key is not None and key in mapping:
            return mapping[key]
    raise RuntimeError(f"settings model lacks {name!r}")


def set_mapping_field(mapping, registry, name, value):
    """Replace one registry-packed map field without changing its key encoding."""
    try:
        numeric = registry.index(name)
    except ValueError:
        numeric = None
    for key in (numeric, name):
        if key is not None and key in mapping:
            mapping[key] = value
            return
    raise RuntimeError(f"settings model lacks {name!r}")


def set_active_template_image(settings, registry, image_data):
    """Copy settings and change only the active MIDI template's image_data field."""
    updated = copy.deepcopy(settings)
    active_name = mapping_field(updated, registry, "active_midi_template")
    templates = mapping_field(updated, registry, "midi_templates")
    if not isinstance(templates, list) or not templates:
        raise RuntimeError("settings contain no MIDI templates")
    matches = [template for template in templates
               if mapping_field(template, registry, "template_name") == active_name]
    if len(matches) != 1:
        raise RuntimeError(f"expected one active MIDI template named {active_name!r}, found {len(matches)}")
    original = mapping_field(matches[0], registry, "image_data")
    if not isinstance(original, str):
        raise RuntimeError(f"active template image_data is {type(original).__name__}, expected string")
    set_mapping_field(matches[0], registry, "image_data", image_data)
    return updated, active_name, original


def build_midi_template_artwork(settings, registry, image_data,
                                template_name="KompleteSynthesia Image Test"):
    """Build one importable named-key .kmt object without changing device settings."""
    named = name_dictionary_keys(settings, registry)
    active_name = named.get("active_midi_template")
    templates = named.get("midi_templates")
    if not isinstance(templates, list):
        raise RuntimeError("settings contain no MIDI template list")
    matches = [template for template in templates
               if isinstance(template, dict) and template.get("template_name") == active_name]
    if len(matches) != 1:
        raise RuntimeError(f"expected one active MIDI template named {active_name!r}, found {len(matches)}")
    artwork = copy.deepcopy(matches[0])
    artwork["template_name"] = template_name
    artwork["image_data"] = image_data
    artwork["hide_template_name"] = True
    return artwork


def prepare_midi_template_image_file(output, template_name="KompleteSynthesia Image Test",
                                     animated=False, fps=30.0, frames=20,
                                     path=SOCKET_PATH):
    """Read the active template and emit a separate custom-artwork .kmt file."""
    if not output.endswith(".kmt"):
        raise RuntimeError("MIDI template output must use the .kmt extension")
    session = Session(path)
    try:
        devices = session.hello.get("available_devices") or []
        if len(devices) != 1:
            raise RuntimeError(f"expected exactly one MK3 device, service reports {len(devices)}")
        serial = devices[0].get("serialnumber")
        settings = request_registered(session, "get_device_settings", [session.uuid, serial])[3]
        artwork = webp_animated_stream(frames, fps) if animated else webp_pattern(0)
        encoded = base64.b64encode(artwork).decode("ascii")
        image_data = "data:image/webp;base64," + encoded
        template = build_midi_template_artwork(settings, session.registry, image_data, template_name)
        with open(output, "w", encoding="utf-8") as destination:
            json.dump(template, destination, indent=2, ensure_ascii=False)
            destination.write("\n")
        print(f"wrote importable template {template_name!r}: {output}", flush=True)
        kind = f"{frames}-frame animated WebP at {fps:g} fps" if animated else "static WebP"
        print(f"embedded artwork data URL length={len(image_data)}; "
              f"source image=1280x212 {kind}", flush=True)
    finally:
        session.close()


def write_settings_backup(settings, agent_version):
    """Persist an exact registry-packed recovery snapshot before a settings write."""
    with tempfile.NamedTemporaryFile(
            mode="wb", prefix="KompleteSynthesia-midi-settings-", suffix=".msgpack",
            dir="/private/tmp", delete=False) as backup:
        backup.write(msgpack.packb({"agent_version": agent_version, "settings": settings}, use_bin_type=True))
        return backup.name


def run_midi_template_image(hold=15.0, confirmed=False, plan_only=False, path=SOCKET_PATH):
    """Temporarily install custom template artwork, then restore exact settings."""
    if not confirmed and not plan_only:
        raise RuntimeError("refusing settings write without --confirmed after operator readiness")
    if not 5 <= hold <= 30:
        raise RuntimeError("MIDI-template image hold must be between 5 and 30 seconds")
    session = Session(path)
    write_attempted = False
    try:
        devices = session.hello.get("available_devices") or []
        if len(devices) != 1:
            raise RuntimeError(f"expected exactly one MK3 device, service reports {len(devices)}")
        serial = devices[0].get("serialnumber")
        original_reply = request_registered(session, "get_device_settings", [session.uuid, serial])
        original = original_reply[3]
        artwork = webp_pattern(0)
        image_data = "data:image/webp;base64," + base64.b64encode(artwork).decode("ascii")
        updated, active_name, original_image = set_active_template_image(
            original, session.registry, image_data)
        print(f"active template={active_name!r}; original image_data length={len(original_image)}; "
              f"test image_data length={len(image_data)}", flush=True)
        if plan_only:
            print("plan-only validation complete; no backup or settings write performed", flush=True)
            return
        backup_path = write_settings_backup(original, session.hello.get("agent_version"))
        print(f"saved exact recovery snapshot: {backup_path}", flush=True)
        write_attempted = True
        request_registered(session, "set_device_settings", [session.uuid, serial, updated])
        print(f"temporary MIDI-template artwork installed for {hold:g}s", flush=True)
        time.sleep(hold)
    finally:
        try:
            if write_attempted:
                request_registered(session, "set_device_settings", [session.uuid, serial, original])
                print("restored exact original device settings", flush=True)
        finally:
            session.close()


def restore_settings_backup(backup_path, confirmed=False, path=SOCKET_PATH):
    """Restore a snapshot emitted by run_midi_template_image after an interrupted run."""
    if not confirmed:
        raise RuntimeError("refusing settings restore without --confirmed")
    with open(backup_path, "rb") as backup:
        saved = msgpack.unpackb(backup.read(), raw=False, strict_map_key=False)
    if not isinstance(saved, dict) or "settings" not in saved:
        raise RuntimeError("not a KompleteSynthesia MIDI settings backup")
    session = Session(path)
    try:
        current_version = session.hello.get("agent_version")
        if saved.get("agent_version") != current_version:
            raise RuntimeError(f"backup agent version {saved.get('agent_version')!r} does not match "
                               f"running version {current_version!r}")
        devices = session.hello.get("available_devices") or []
        if len(devices) != 1:
            raise RuntimeError(f"expected exactly one MK3 device, service reports {len(devices)}")
        serial = devices[0].get("serialnumber")
        request_registered(session, "set_device_settings", [session.uuid, serial, saved["settings"]])
        print(f"restored device settings from {backup_path}", flush=True)
    finally:
        session.close()


def webp_pattern(index, width=1280, height=212):
    """Generate two deterministic WebPs which cannot be mistaken for NI artwork."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise RuntimeError("display probing requires Pillow (`python3 -m pip install Pillow`)") from e
    colors = [(235, 30, 90), (15, 190, 225)] if index == 0 else [(35, 210, 95), (100, 35, 210)]
    image = Image.new("RGB", (width, height), colors[0])
    draw = ImageDraw.Draw(image)
    block = 53
    for y in range(0, height, block):
        for x in range(0, width, block):
            if ((x // block) + (y // block)) % 2:
                draw.rectangle((x, y, x + block - 1, y + block - 1), fill=colors[1])
    draw.rectangle((330, 58, 950, 154), fill=(0, 0, 0))
    draw.text((410, 92), f"KOMPLETE SYNTHESIA ODR FRAME {index + 1}", fill=(255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="WEBP", lossless=True, method=6)
    return output.getvalue()


def webp_upload_probe(nonce, width=1280, height=212):
    """Generate a deterministic, nonce-specific static asset for one upload trace."""
    if not isinstance(nonce, str) or not 1 <= len(nonce) <= 32:
        raise ValueError("upload probe nonce must contain 1 to 32 characters")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for character in nonce):
        raise ValueError("upload probe nonce may contain only ASCII letters, digits, and hyphens")
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise RuntimeError("display probing requires Pillow (`python3 -m pip install Pillow`)") from e
    seed = hashlib.sha256(nonce.encode("ascii")).digest()
    image = Image.new("RGB", (width, height), (8, 12, 24))
    draw = ImageDraw.Draw(image)
    for index, x in enumerate(range(0, width, 64)):
        color = (40 + seed[index % 32] // 2, 30 + seed[(index + 7) % 32] // 3,
                 50 + seed[(index + 13) % 32] // 2)
        draw.rectangle((x, 0, min(width - 1, x + 63), height - 1), fill=color)
    draw.rectangle((250, 55, 1030, 157), fill=(0, 0, 0))
    draw.text((350, 83), f"ONE ASSET UPLOAD: {nonce}", fill=(255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="WEBP", lossless=True, method=6)
    return output.getvalue()


def webp_stream_frame(index, count, width=1280, height=212):
    """Generate one unique, visibly sequential piano-roll-style WebP frame."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise RuntimeError("display probing requires Pillow (`python3 -m pip install Pillow`)") from e
    if count < 2 or not 0 <= index < count:
        raise ValueError("stream frame index must be inside a sequence of at least two frames")
    image = Image.new("RGB", (width, height), (5, 9, 20))
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 80):
        draw.line((x, 0, x, height), fill=(25, 40, 65), width=1)
    for y in range(0, height, 53):
        draw.line((0, y, width, y), fill=(25, 40, 65), width=1)
    progress_x = round(index * (width - 1) / (count - 1))
    draw.rectangle((max(0, progress_x - 8), 0, min(width - 1, progress_x + 8), height - 1),
                   fill=(245, 55, 105))
    for note in range(18):
        x = (note * 173 - index * 37) % (width + 180) - 90
        y = 20 + (note * 47) % 150
        color = (20, 205, 235) if note % 2 else (90, 230, 110)
        draw.rounded_rectangle((x, y, x + 110, y + 20), radius=5, fill=color)
    draw.rectangle((450, 70, 830, 142), fill=(0, 0, 0))
    draw.text((525, 91), f"UNIQUE ODR STREAM {index + 1:02d}/{count:02d}", fill=(255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="WEBP", lossless=True, method=6)
    return output.getvalue()


def webp_animated_stream(count=20, fps=30.0, width=1280, height=212):
    """Encode the visible stream sequence as one looping animated WebP asset."""
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("display probing requires Pillow (`python3 -m pip install Pillow`)") from e
    if not 2 <= count <= 20:
        raise ValueError("animated stream frame count must be between 2 and 20")
    if not 0 < fps <= 60:
        raise ValueError("animated stream rate must be above zero and no higher than 60 fps")
    frames = []
    for index in range(count):
        with Image.open(io.BytesIO(webp_stream_frame(index, count, width, height))) as image:
            frames.append(image.convert("RGB"))
    output = io.BytesIO()
    frames[0].save(output, format="WEBP", save_all=True, append_images=frames[1:],
                   duration=max(1, round(1000 / fps)), loop=0, lossless=True, method=6)
    return output.getvalue()


def load_schema(path):
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    required = {"add_asset", "show_asset"}
    missing = required - set(schema)
    if missing:
        raise RuntimeError(f"schema lacks {', '.join(sorted(missing))}; derive it from a capture first")
    return schema


def substitute(value, variables, session):
    if isinstance(value, str) and value.startswith("$"):
        key = value[1:]
        if key == "uuid":
            return session.uuid
        if key.startswith("symbol:"):
            return session.resolve(key.split(":", 1)[1])
        if key not in variables:
            raise RuntimeError(f"schema references unknown variable {value}")
        return variables[key]
    if isinstance(value, list):
        return [substitute(v, variables, session) for v in value]
    if isinstance(value, dict):
        return {substitute(k, variables, session): substitute(v, variables, session) for k, v in value.items()}
    return value


def send_template(session, template, variables):
    method = substitute(template["method"], variables, session)
    params = substitute(template["params"], variables, session)
    if template.get("kind", "notification") == "request":
        return session.request(method, params)
    session.notify(method, params)
    return None


def prepare_display_session(session, schema):
    devices = session.hello.get("available_devices") or []
    if len(devices) != 1:
        raise RuntimeError(f"expected exactly one MK3 device, service reports {len(devices)}")
    serial = devices[0].get("serialnumber")
    variables = {"serial": serial}
    connect = {"kind": "request", "method": "$symbol:connect_device", "params": ["$uuid", "$serial"]}
    focus = {"method": "$symbol:client_request_focus", "params": ["$uuid", "$serial"]}
    send_template(session, connect, variables)
    send_template(session, focus, variables)
    # Focus and asset writes are notifications: acceptance has no reply and the
    # device bridge processes them asynchronously. Do not close the session while
    # either is still queued.
    time.sleep(0.3)
    for template in schema.get("prepare", []):
        send_template(session, template, variables)
    return variables


def run_display(schema_path, refresh=False, duration=12.0, hold=45.0):
    schema = load_schema(schema_path)
    session = Session()
    try:
        variables = prepare_display_session(session, schema)
        assets = []
        for index in range(2):
            data = webp_pattern(index)
            digest = hashlib.sha256(data).digest()
            local = dict(variables, asset=data, asset_sha256=digest, asset_hex=digest.hex())
            send_template(session, schema["add_asset"], local)
            print(f"uploaded frame {index + 1}: {digest.hex()} ({len(data)} bytes)")
            assets.append(local)
            time.sleep(0.5)
        rates = [0.5, 1, 2, 5, 10] if refresh else [0]
        for rate in rates:
            if refresh:
                input(f"Press Enter when the operator is ready to start {rate:g} fps (Ctrl-C to stop safely)...")
            switches = 1 if rate == 0 else max(2, int(duration * rate))
            started = time.monotonic()
            for n in range(switches):
                send_template(session, schema["show_asset"], assets[n % 2])
                if rate:
                    deadline = started + (n + 1) / rate
                    time.sleep(max(0, deadline - time.monotonic()))
            elapsed = time.monotonic() - started
            print(f"requested {rate or 'static'} fps: {switches} switches in {elapsed:.3f}s")
        if not refresh:
            print(f"holding ODR focus for {hold:g}s; inspect the keyboard now", flush=True)
            time.sleep(hold)
        else:
            time.sleep(1.0)
    finally:
        session.close()


def run_trace_static(schema_path, hold=3.0, confirmed=False, plan_only=False, upload_nonce=None):
    """Select one already-cached test asset for endpoint-4 correlation capture."""
    if not 0 <= hold <= 10:
        raise RuntimeError("trace-static hold must be between 0 and 10 seconds")
    schema = load_schema(schema_path)
    data = webp_upload_probe(upload_nonce) if upload_nonce else webp_pattern(0)
    digest = hashlib.sha256(data).digest()
    session = Session()
    try:
        registered = set(session.hello.get("registered_assets") or [])
        if digest not in registered:
            raise RuntimeError("trace-static refuses to upload; its test asset is not already cached")
        label = f"upload probe {upload_nonce!r}" if upload_nonce else "checkerboard"
        print(f"trace-static {label} is cached: sha256={digest.hex()} ({len(data)} bytes)")
        print("planned writes: connect_device request, focus, parameter page selection, "
              "one parameter-page model with the cached asset digest")
        if plan_only:
            print("plan-only complete; no focus, page, asset, or display notification was sent")
            return
        if not confirmed:
            raise RuntimeError("trace-static requires --confirmed after the operator says they are watching")
        variables = prepare_display_session(session, schema)
        local = dict(variables, asset=data, asset_sha256=digest, asset_hex=digest.hex())
        send_template(session, schema["show_asset"], local)
        print("selected one cached static asset; no add_asset or second display model was sent", flush=True)
        time.sleep(hold)
    finally:
        session.close()


def run_trace_upload(schema_path, nonce, hold=3.0, confirmed=False, plan_only=False):
    """Upload and select exactly one previously unregistered asset for USB correlation."""
    if not 0 <= hold <= 10:
        raise RuntimeError("trace-upload hold must be between 0 and 10 seconds")
    if not confirmed and not plan_only:
        raise RuntimeError("trace-upload requires --confirmed after the operator says they are watching")
    schema = load_schema(schema_path)
    data = webp_upload_probe(nonce)
    if len(data) > 256 * 1024:
        raise RuntimeError("trace-upload asset exceeds its 256 KiB payload cap")
    digest = hashlib.sha256(data).digest()
    session = Session()
    try:
        registered = set(session.hello.get("registered_assets") or [])
        if digest in registered:
            raise RuntimeError("trace-upload requires a new asset, but this nonce is already cached")
        print(f"trace-upload asset is new: sha256={digest.hex()} ({len(data)} bytes)")
        print("planned writes: connect_device request, focus, parameter page selection, "
              "one add_asset notification, and one parameter-page model selecting its digest")
        if plan_only:
            print("plan-only complete; no focus, page, asset, or display notification was sent")
            return
        variables = prepare_display_session(session, schema)
        local = dict(variables, asset=data, asset_sha256=digest, asset_hex=digest.hex())
        send_template(session, schema["add_asset"], local)
        time.sleep(0.4)
        send_template(session, schema["show_asset"], local)
        print("uploaded and selected one new static asset; no second asset or model was sent", flush=True)
        time.sleep(hold)
    finally:
        session.close()


def native_parameter_model(session):
    """Build the minimal captured NKS1 eight-knob model with no image background."""
    symbol = session.resolve
    parameters = {}
    labels = ["MOTION", "STATIC 2", "STATIC 3", "STATIC 4",
              "STATIC 5", "STATIC 6", "STATIC 7", "STATIC 8"]
    for index, label in enumerate(labels):
        parameters[index] = {
            "continuous_parameter": {
                "parameter_style": {"knob_parameter_style": None},
                "parameter_value": {
                    "display_value": "0%",
                    "value": 0.0,
                },
            }
        }
    color = {
        symbol("r"): 30,
        symbol("g"): 205,
        symbol("b"): 235,
        symbol("is_rgb565"): False,
    }
    layout_parameters = [
        {symbol("id"): index, symbol("name"): label}
        for index, label in enumerate(labels)
    ]
    return {
        symbol("plugin_data"): {
            symbol("background"): None,
            symbol("parameters"): parameters,
            symbol("plugin_color"): color,
            symbol("nks_control_color"): color,
            symbol("name"): "KompleteSynthesia Native Motion",
        },
        symbol("device_owned_viewstate"): {
            symbol("view_address"): {symbol("nks1_view_address"): 0},
            symbol("force_host_viewstate"): False,
        },
        symbol("host_owned_viewstate"): {
            symbol("nks1_layout"): [[{
                symbol("section_name"): "",
                symbol("parameters"): layout_parameters,
            }]],
            symbol("nks2_layout"): None,
        },
    }


def native_plugin_chain_model(session):
    """Build the minimum captured non-empty chain needed to render parameters."""
    symbol = session.resolve
    plugin_identifier = hashlib.sha256(b"KompleteSynthesia Native Motion").digest()[:16]
    empty_identifier = bytes(16)

    def entry(name, identifier):
        return {
            symbol("type"): None,
            symbol("can_bypass"): False,
            symbol("bypass_enabled"): False,
            symbol("can_remove"): False,
            symbol("can_move_forward"): False,
            symbol("can_move_backward"): False,
            symbol("supported_nks_version"): None,
            symbol("name"): name,
            symbol("identifier"): identifier,
        }

    return {
        symbol("plugins"): [
            entry("KompleteSynthesia Native Motion", plugin_identifier),
            entry("Empty", empty_identifier),
        ],
        symbol("current_plugin_index"): 0,
    }


def triangle_value(elapsed, period=2.0):
    if period <= 0:
        raise ValueError("triangle period must be positive")
    phase = (elapsed / period) % 1.0
    return 1.0 - abs(2.0 * phase - 1.0)


def service_log_cursor(path=SERVICE_LOG_PATH):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def assert_no_session_parse_errors(session_uuid, cursor, path=SERVICE_LOG_PATH):
    """Fail a guarded probe if HCS logged a parser rejection for this session."""
    if cursor is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            stream.seek(cursor)
            added = stream.read()
            next_cursor = stream.tell()
    except OSError:
        return None
    rejected = [line for line in added.splitlines()
                if session_uuid in line and "Arguments cannot be parsed" in line]
    if rejected:
        raise RuntimeError(f"HCS rejected a guarded native-renderer message: {rejected[0]}")
    return next_cursor


def run_native_parameter_ramp(schema_path, fps=30.0, duration=6.0,
                              confirmed=False, plan_only=False, all_knobs=False):
    """Animate one (or all eight) device-native knobs without uploading or replacing
    any image. With all_knobs, each knob is phase-staggered across the same triangle
    period so simultaneous, independent motion is visually obvious rather than eight
    knobs moving in lockstep."""
    if not 1 <= fps <= 60:
        raise RuntimeError("native-ramp fps must be between 1 and 60")
    if not 1 <= duration <= 10:
        raise RuntimeError("native-ramp duration must be between 1 and 10 seconds")
    if not confirmed and not plan_only:
        raise RuntimeError("native-ramp requires --confirmed after the operator says they are watching")
    knob_indices = range(8) if all_knobs else (0,)
    period = 2.0
    schema = load_schema(schema_path)
    session = Session()
    try:
        model = native_parameter_model(session)
        plugin_chain = native_plugin_chain_model(session)
        update_method = session.resolve("client_parameter_page_set_parameter_value")
        log_cursor = service_log_cursor()
        knob_desc = "all eight knobs, phase-staggered" if all_knobs else "first knob"
        print(f"planned native renderer test: one synthetic plugin-chain entry, eight knobs, "
              f"{knob_desc} triangle ramp, {fps:g} updates/s for {duration:g}s; "
              "no asset upload or image selection")
        if plan_only:
            print("plan-only complete; no focus, page, model, or parameter update was sent")
            return
        variables = prepare_display_session(session, schema)
        session.notify(session.resolve("client_parameter_page_set_data"),
                       [session.uuid, variables["serial"], model])
        session.notify(session.resolve("client_plugin_chain_set_data"),
                       [session.uuid, variables["serial"], plugin_chain])
        time.sleep(0.8)
        log_cursor = assert_no_session_parse_errors(session.uuid, log_cursor)
        session.notify(update_method, [
            session.uuid,
            variables["serial"],
            0,
            {"continuous_parameter": {"value": 0.0, "display_value": "  0%"}},
        ])
        time.sleep(0.15)
        log_cursor = assert_no_session_parse_errors(session.uuid, log_cursor)
        updates = max(1, round(fps * duration))
        started = time.monotonic()
        late = 0
        for tick in range(updates):
            elapsed = tick / fps
            for knob in knob_indices:
                value = triangle_value(elapsed + knob * period / 8, period)
                session.notify(update_method, [
                    session.uuid,
                    variables["serial"],
                    knob,
                    {"continuous_parameter": {
                        "value": value,
                        "display_value": f"{round(value * 100):3d}%",
                    }},
                ])
            deadline = started + (tick + 1) / fps
            if time.monotonic() > deadline:
                late += 1
            time.sleep(max(0, deadline - time.monotonic()))
        elapsed = time.monotonic() - started
        assert_no_session_parse_errors(session.uuid, log_cursor)
        print(f"native knob ramp dispatched {updates} ticks "
              f"({updates * len(knob_indices)} messages) in {elapsed:.3f}s; "
              f"late_deadlines={late}", flush=True)
        time.sleep(1.0)
    finally:
        session.close()


def run_unique_stream(schema_path, fps=10.0, frames=20, cycles=1, payload_cap=4 * 1024 * 1024):
    """Upload and immediately show a bounded sequence of unique frames."""
    if not 0 < fps <= 10:
        raise RuntimeError("unique-stream rate must be above zero and no higher than the verified 10 fps")
    if not 2 <= frames <= 20:
        raise RuntimeError("unique-stream frame count must be between 2 and the safety limit of 20")
    if not 1 <= cycles <= 10:
        raise RuntimeError("unique-stream cycle count must be between 1 and 10")
    schema = load_schema(schema_path)
    encoded = [webp_stream_frame(index, frames) for index in range(frames)]
    digests = [hashlib.sha256(data).digest() for data in encoded]
    if len(set(digests)) != frames:
        raise RuntimeError("generated stream frames were not content-unique")
    total_bytes = sum(map(len, encoded))
    if total_bytes > payload_cap:
        raise RuntimeError(f"encoded stream is {total_bytes} bytes, above the {payload_cap}-byte safety cap")

    session = Session()
    before_count = len(session.hello.get("registered_assets") or [])
    try:
        variables = prepare_display_session(session, schema)
        print(f"prepared {frames} unique frames: {total_bytes} bytes total; inventory before={before_count}")
        display_count = frames * cycles
        input(f"Press Enter when the operator is ready for {display_count / fps:.1f}s "
              f"at {fps:g} fps ({cycles} cached cycle{'s' if cycles != 1 else ''})...")
        started = time.monotonic()
        late = 0
        largest_send = 0.0
        for display_index in range(display_count):
            frame_index = display_index % frames
            data = encoded[frame_index]
            digest = digests[frame_index]
            local = dict(variables, asset=data, asset_sha256=digest, asset_hex=digest.hex())
            send_started = time.monotonic()
            send_template(session, schema["add_asset"], local)
            send_template(session, schema["show_asset"], local)
            send_elapsed = time.monotonic() - send_started
            largest_send = max(largest_send, send_elapsed)
            deadline = started + (display_index + 1) / fps
            if time.monotonic() > deadline:
                late += 1
            time.sleep(max(0, deadline - time.monotonic()))
        elapsed = time.monotonic() - started
        print(f"unique stream dispatched {display_count} frames ({frames} unique) in {elapsed:.3f}s; "
              f"late_deadlines={late}; largest_send={largest_send * 1000:.3f}ms")
        time.sleep(2.0)
    finally:
        session.close()

    post = Session()
    try:
        after_count = len(post.hello.get("registered_assets") or [])
    finally:
        post.close()
    print(f"inventory after reconnect={after_count}; delta={after_count - before_count}")


def run_cached_cadence(schema_path, fps=10.0, frames=20, cycles=5, show_key="show_asset"):
    """Show pre-existing assets without sending add_asset during the timed run."""
    if not 0 < fps <= 30:
        raise RuntimeError("cached-cadence rate must be above zero and no higher than 30 fps")
    if not 2 <= frames <= 20:
        raise RuntimeError("cached-cadence frame count must be between 2 and 20")
    if not 1 <= cycles <= 10:
        raise RuntimeError("cached-cadence cycle count must be between 1 and 10")
    schema = load_schema(schema_path)
    if show_key not in schema:
        raise RuntimeError(f"schema lacks requested display template {show_key!r}")
    encoded = [webp_stream_frame(index, frames) for index in range(frames)]
    digests = [hashlib.sha256(data).digest() for data in encoded]
    session = Session()
    registered = set(session.hello.get("registered_assets") or [])
    missing = [digest.hex() for digest in digests if digest not in registered]
    if missing:
        session.close()
        raise RuntimeError(f"cached-cadence refuses to upload; {len(missing)} required assets are absent")
    try:
        variables = prepare_display_session(session, schema)
        prime = dict(variables, asset=encoded[0], asset_sha256=digests[0], asset_hex=digests[0].hex())
        send_template(session, schema["show_asset"], prime)
        time.sleep(0.5)
        display_count = frames * cycles
        print(f"verified {frames} frames already cached; timed phase uses {show_key} only")
        input(f"Press Enter when the operator is ready for {display_count / fps:.1f}s "
              f"at {fps:g} fps ({cycles} cycle{'s' if cycles != 1 else ''})...")
        started = time.monotonic()
        late = 0
        largest_send = 0.0
        for display_index in range(display_count):
            frame_index = display_index % frames
            local = dict(variables, asset=encoded[frame_index],
                         asset_sha256=digests[frame_index], asset_hex=digests[frame_index].hex())
            send_started = time.monotonic()
            send_template(session, schema[show_key], local)
            send_elapsed = time.monotonic() - send_started
            largest_send = max(largest_send, send_elapsed)
            deadline = started + (display_index + 1) / fps
            if time.monotonic() > deadline:
                late += 1
            time.sleep(max(0, deadline - time.monotonic()))
        elapsed = time.monotonic() - started
        print(f"cached cadence dispatched {display_count} selections in {elapsed:.3f}s; "
              f"late_deadlines={late}; largest_send={largest_send * 1000:.3f}ms")
        time.sleep(2.0)
    finally:
        session.close()


def run_animated_asset(schema_path, fps=30.0, frames=20, hold=8.0,
                       show_key="show_asset_plugin_data", payload_cap=4 * 1024 * 1024):
    """Upload and select one looping animated WebP without per-frame ODR calls."""
    if not 0 < fps <= 60:
        raise RuntimeError("animated-asset rate must be above zero and no higher than 60 fps")
    if not 2 <= frames <= 20:
        raise RuntimeError("animated-asset frame count must be between 2 and 20")
    if not 1 <= hold <= 30:
        raise RuntimeError("animated-asset hold must be between 1 and 30 seconds")
    schema = load_schema(schema_path)
    if show_key not in schema:
        raise RuntimeError(f"schema lacks requested display template {show_key!r}")
    data = webp_animated_stream(frames, fps)
    if len(data) > payload_cap:
        raise RuntimeError(f"animated WebP is {len(data)} bytes, above the {payload_cap}-byte safety cap")
    digest = hashlib.sha256(data).digest()

    session = Session()
    before_count = len(session.hello.get("registered_assets") or [])
    try:
        variables = prepare_display_session(session, schema)
        local = dict(variables, asset=data, asset_sha256=digest, asset_hex=digest.hex())
        input(f"Press Enter when the operator is ready to show one {frames}-frame animated WebP "
              f"at {fps:g} fps for {hold:g}s...")
        send_template(session, schema["add_asset"], local)
        time.sleep(0.75)
        send_template(session, schema[show_key], local)
        print(f"selected one animated WebP ({len(data)} bytes, sha256={digest.hex()}); "
              "no per-frame ODR calls will be sent", flush=True)
        time.sleep(hold)
    finally:
        session.close()

    post = Session()
    try:
        after_count = len(post.hello.get("registered_assets") or [])
    finally:
        post.close()
    print(f"inventory after reconnect={after_count}; delta={after_count - before_count}")


def run_browser_cadence(schema_path, fps=30.0, frames=20, cycles=10, image_slot="sound"):
    """Update one browser image-bearing model using already-cached assets."""
    if not 0 < fps <= 30:
        raise RuntimeError("browser-cadence rate must be above zero and no higher than 30 fps")
    if not 2 <= frames <= 20:
        raise RuntimeError("browser-cadence frame count must be between 2 and 20")
    if not 1 <= cycles <= 10:
        raise RuntimeError("browser-cadence cycle count must be between 1 and 10")
    schema = load_schema(schema_path)
    if image_slot not in {"sound", "product"}:
        raise RuntimeError(f"unknown browser image slot {image_slot!r}")
    required = {"prepare_browser", "show_browser_asset", "update_browser_asset"} if image_slot == "sound" else {
        "prepare_browser", "show_browser_product"
    }
    missing_templates = required - set(schema)
    if missing_templates:
        raise RuntimeError(f"schema lacks {', '.join(sorted(missing_templates))}")
    encoded = [webp_stream_frame(index, frames) for index in range(frames)]
    digests = [hashlib.sha256(data).digest() for data in encoded]

    session = Session()
    registered = set(session.hello.get("registered_assets") or [])
    missing_assets = [digest.hex() for digest in digests if digest not in registered]
    if missing_assets:
        session.close()
        raise RuntimeError(f"browser cadence refuses to upload; {len(missing_assets)} required assets are absent")
    try:
        variables = prepare_display_session(session, schema)
        for template in schema["prepare_browser"]:
            send_template(session, template, variables)
        first = dict(variables, asset=encoded[0], asset_sha256=digests[0], asset_hex=digests[0].hex())
        initial_key = "show_browser_asset" if image_slot == "sound" else "show_browser_product"
        update_key = "update_browser_asset" if image_slot == "sound" else "show_browser_product"
        send_template(session, schema[initial_key], first)
        time.sleep(0.75)
        display_count = frames * cycles
        print(f"browser {image_slot} image slot initialized; verified {frames} frames already cached")
        input(f"Press Enter when the operator is ready for {display_count / fps:.1f}s "
              f"at {fps:g} fps ({cycles} cycle{'s' if cycles != 1 else ''})...")
        started = time.monotonic()
        late = 0
        largest_send = 0.0
        for display_index in range(display_count):
            frame_index = display_index % frames
            local = dict(variables, asset=encoded[frame_index],
                         asset_sha256=digests[frame_index], asset_hex=digests[frame_index].hex())
            send_started = time.monotonic()
            send_template(session, schema[update_key], local)
            send_elapsed = time.monotonic() - send_started
            largest_send = max(largest_send, send_elapsed)
            deadline = started + (display_index + 1) / fps
            if time.monotonic() > deadline:
                late += 1
            time.sleep(max(0, deadline - time.monotonic()))
        elapsed = time.monotonic() - started
        print(f"browser cadence dispatched {display_count} item updates in {elapsed:.3f}s; "
              f"late_deadlines={late}; largest_send={largest_send * 1000:.3f}ms")
        time.sleep(2.0)
    finally:
        session.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="summarize display-related capture traffic")
    inspect.add_argument("capture")
    inspect.add_argument("--output")
    sub.add_parser("inventory", help="print the service's registered asset inventory")
    wait_device = sub.add_parser("wait-device", help="wait read-only for one registered MK3 device")
    wait_device.add_argument("--timeout", type=float, default=15.0)
    sub.add_parser("settings-inventory", help="read and summarize MIDI-template image settings")
    midi_image = sub.add_parser("midi-template-image", help="temporarily show custom MIDI-template artwork")
    midi_image.add_argument("--hold", type=float, default=15.0)
    midi_image.add_argument("--confirmed", action="store_true",
                            help="confirm the operator is watching and settings may be temporarily changed")
    midi_image.add_argument("--plan-only", action="store_true",
                            help="validate against live settings without writing or changing the display")
    prepare_template = sub.add_parser(
        "prepare-midi-template-image", help="write a separate importable custom-artwork .kmt file")
    prepare_template.add_argument("--output", required=True)
    prepare_template.add_argument("--name", default="KompleteSynthesia Image Test")
    prepare_template.add_argument("--animated", action="store_true")
    prepare_template.add_argument("--fps", type=float, default=30.0)
    prepare_template.add_argument("--frames", type=int, default=20)
    restore = sub.add_parser("restore-settings", help="restore an interrupted MIDI-template settings snapshot")
    restore.add_argument("backup")
    restore.add_argument("--confirmed", action="store_true")
    static = sub.add_parser("static", help="show two captured-schema WebP test assets")
    static.add_argument("--schema", required=True)
    static.add_argument("--hold", type=float, default=45.0)
    trace_static = sub.add_parser("trace-static", help="select one cached asset for raw USB correlation")
    trace_static.add_argument("--schema", required=True)
    trace_static.add_argument("--hold", type=float, default=3.0)
    trace_static.add_argument("--confirmed", action="store_true")
    trace_static.add_argument("--plan-only", action="store_true")
    trace_static.add_argument("--upload-nonce",
                              help="select a cached upload-probe image instead of the checkerboard")
    trace_upload = sub.add_parser("trace-upload", help="upload and select one new asset for USB correlation")
    trace_upload.add_argument("--schema", required=True)
    trace_upload.add_argument("--nonce", required=True)
    trace_upload.add_argument("--hold", type=float, default=3.0)
    trace_upload.add_argument("--confirmed", action="store_true")
    trace_upload.add_argument("--plan-only", action="store_true")
    native_ramp = sub.add_parser("native-ramp", help="animate one device-native knob without images")
    native_ramp.add_argument("--schema", required=True)
    native_ramp.add_argument("--fps", type=float, default=30.0)
    native_ramp.add_argument("--duration", type=float, default=6.0)
    native_ramp.add_argument("--all-knobs", action="store_true",
                             help="animate all eight knobs at once, phase-staggered, "
                                  "instead of only knob 0")
    native_ramp.add_argument("--confirmed", action="store_true")
    native_ramp.add_argument("--plan-only", action="store_true")
    refresh = sub.add_parser("refresh", help="alternate two assets at a rate ladder")
    refresh.add_argument("--schema", required=True)
    refresh.add_argument("--duration", type=float, default=12.0)
    stream = sub.add_parser("stream", help="upload and show a bounded sequence of unique WebP frames")
    stream.add_argument("--schema", required=True)
    stream.add_argument("--fps", type=float, default=10.0)
    stream.add_argument("--frames", type=int, default=20)
    stream.add_argument("--cycles", type=int, default=1)
    cadence = sub.add_parser("cadence", help="show an already-cached frame sequence without asset writes")
    cadence.add_argument("--schema", required=True)
    cadence.add_argument("--fps", type=float, default=10.0)
    cadence.add_argument("--frames", type=int, default=20)
    cadence.add_argument("--cycles", type=int, default=5)
    cadence.add_argument("--show-template", choices=("show_asset", "show_asset_plugin_data"),
                         default="show_asset")
    animated = sub.add_parser("animated", help="show one looping animated WebP with no frame swaps")
    animated.add_argument("--schema", required=True)
    animated.add_argument("--fps", type=float, default=30.0)
    animated.add_argument("--frames", type=int, default=20)
    animated.add_argument("--hold", type=float, default=8.0)
    animated.add_argument("--show-template", choices=("show_asset", "show_asset_plugin_data"),
                          default="show_asset_plugin_data")
    browser = sub.add_parser("browser-cadence", help="update one cached browser item image")
    browser.add_argument("--schema", required=True)
    browser.add_argument("--fps", type=float, default=30.0)
    browser.add_argument("--frames", type=int, default=20)
    browser.add_argument("--cycles", type=int, default=10)
    browser.add_argument("--image-slot", choices=("sound", "product"), default="sound")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "inspect":
        inspect_capture(args.capture, args.output)
    elif args.command == "inventory":
        inventory()
    elif args.command == "wait-device":
        wait_for_device(args.timeout)
    elif args.command == "settings-inventory":
        settings_inventory()
    elif args.command == "midi-template-image":
        run_midi_template_image(args.hold, args.confirmed, args.plan_only)
    elif args.command == "prepare-midi-template-image":
        prepare_midi_template_image_file(
            args.output, args.name, args.animated, args.fps, args.frames)
    elif args.command == "restore-settings":
        restore_settings_backup(args.backup, args.confirmed)
    elif args.command == "static":
        run_display(args.schema, hold=args.hold)
    elif args.command == "trace-static":
        run_trace_static(args.schema, args.hold, args.confirmed, args.plan_only, args.upload_nonce)
    elif args.command == "trace-upload":
        run_trace_upload(args.schema, args.nonce, args.hold, args.confirmed, args.plan_only)
    elif args.command == "native-ramp":
        run_native_parameter_ramp(args.schema, args.fps, args.duration,
                                  args.confirmed, args.plan_only, args.all_knobs)
    elif args.command == "refresh":
        run_display(args.schema, True, args.duration)
    elif args.command == "stream":
        run_unique_stream(args.schema, args.fps, args.frames, args.cycles)
    elif args.command == "cadence":
        run_cached_cadence(args.schema, args.fps, args.frames, args.cycles, args.show_template)
    elif args.command == "animated":
        run_animated_asset(args.schema, args.fps, args.frames, args.hold, args.show_template)
    elif args.command == "browser-cadence":
        run_browser_cadence(args.schema, args.fps, args.frames, args.cycles, args.image_slot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
