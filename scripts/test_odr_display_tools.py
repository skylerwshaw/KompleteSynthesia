#!/usr/bin/env python3
import hashlib
import io
import json
import os
import socket
import struct
import sys
import tempfile
import unittest
import uuid

import msgpack

sys.path.insert(0, os.path.dirname(__file__))
import odr_display_probe as probe
import odr_relay_capture as relay


class RelayTests(unittest.TestCase):
    def test_fragmented_preamble_and_frames_resolve_method(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "capture.jsonl")
            capture = relay.Capture(output, os.path.join(directory, "blobs"), True, stream)
            state = relay.ConnectionState()
            server = relay.Direction(1, "service->client", False, capture, state)
            registry = ["zero", "add_asset"]
            reply = [1, 1, None, {"symbol_registry": registry, "serialnumber": "SECRET"}]
            packet = probe.frame(reply)
            server.feed(packet[:3])
            server.feed(packet[3:])
            client = relay.Direction(1, "client->service", True, capture, state)
            identity = uuid.UUID("18bc0664-8278-42dc-ba80-0f9584cb290b")
            request = probe.frame([0, 2, 1, [b"payload"]])
            client.feed(identity.bytes[:7])
            client.feed(identity.bytes[7:] + request[:2])
            client.feed(request[2:])
            capture.close()
            with open(output, encoding="utf-8") as f:
                captured_text = f.read()
            records = [json.loads(line) for line in captured_text.splitlines()]
            frame_record = records[-1]
            self.assertEqual(frame_record["method_name"], "add_asset")
            self.assertEqual(frame_record["message"][3][0]["$binary"]["length"], 7)
            self.assertNotIn("SECRET", captured_text)

    def test_restore_refuses_live_pid_and_restores_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = os.path.join(directory, "service")
            real_path = socket_path + ".real"
            pid_path = socket_path + ".pid"
            open(real_path, "wb").close()
            with open(pid_path, "w", encoding="ascii") as f:
                f.write(str(os.getpid()))
            ok, _ = relay.restore_stale(socket_path, real_path, pid_path)
            self.assertFalse(ok)
            with open(pid_path, "w", encoding="ascii") as f:
                f.write("99999999")
            open(socket_path, "wb").close()
            ok, _ = relay.restore_stale(socket_path, real_path, pid_path)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(socket_path))
            self.assertFalse(os.path.exists(real_path))


class ProbeTests(unittest.TestCase):
    def test_patterns_are_stable_distinct_webps(self):
        first = probe.webp_pattern(0)
        second = probe.webp_pattern(1)
        self.assertTrue(first.startswith(b"RIFF") and first[8:12] == b"WEBP")
        self.assertTrue(second.startswith(b"RIFF") and second[8:12] == b"WEBP")
        self.assertNotEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
        self.assertEqual(first, probe.webp_pattern(0))

    def test_upload_probe_is_nonce_specific_stable_and_bounded(self):
        first = probe.webp_upload_probe("usb-upload-20260812-a")
        second = probe.webp_upload_probe("usb-upload-20260812-b")
        self.assertEqual(first, probe.webp_upload_probe("usb-upload-20260812-a"))
        self.assertNotEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
        self.assertLess(len(first), 256 * 1024)

    def test_upload_probe_rejects_unsafe_nonce(self):
        with self.assertRaises(ValueError):
            probe.webp_upload_probe("has spaces")
        with self.assertRaises(ValueError):
            probe.webp_upload_probe("")

    def test_trace_upload_requires_confirmation_before_connecting(self):
        with self.assertRaisesRegex(RuntimeError, "--confirmed"):
            probe.run_trace_upload("not-used", "safe-nonce", confirmed=False)

    def test_native_ramp_requires_confirmation_before_connecting(self):
        with self.assertRaisesRegex(RuntimeError, "--confirmed"):
            probe.run_native_parameter_ramp("not-used", confirmed=False)

    def test_native_ramp_triangle_is_two_second_zero_to_one_cycle(self):
        values = [probe.triangle_value(point) for point in (0, 0.5, 1, 1.5, 2)]
        self.assertEqual(values, [0.0, 0.5, 1.0, 0.5, 0.0])
        with self.assertRaises(ValueError):
            probe.triangle_value(0, period=0)

    def test_native_models_use_captured_layout_id_and_nonempty_chain(self):
        class NamedSession:
            @staticmethod
            def resolve(name):
                return name

        model = probe.native_parameter_model(NamedSession())
        layout = model["host_owned_viewstate"]["nks1_layout"]
        first = layout[0][0]["parameters"][0]
        self.assertEqual(first, {"id": 0, "name": "MOTION"})
        self.assertNotIn("parameter_index", first)
        chain = probe.native_plugin_chain_model(NamedSession())
        self.assertEqual(chain["current_plugin_index"], 0)
        self.assertEqual(chain["plugins"][0]["name"], "KompleteSynthesia Native Motion")
        self.assertEqual(len(chain["plugins"][0]["identifier"]), 16)
        parameter = model["plugin_data"]["parameters"][0]
        self.assertEqual(parameter, {
            "continuous_parameter": {
                "parameter_style": {"knob_parameter_style": None},
                "parameter_value": {"display_value": "0%", "value": 0.0},
            }
        })

    def test_all_knobs_phase_stagger_spreads_peaks_across_the_period(self):
        period = 2.0
        # Mirrors run_native_parameter_ramp's per-knob offset: knob*period/8.
        values = [probe.triangle_value(0 + knob * period / 8, period) for knob in range(8)]
        self.assertEqual(values[0], 0.0)
        self.assertEqual(values[4], 1.0)  # opposite phase, at the far end of the row
        # A triangle wave folds, so opposite-phase pairs (e.g. knob 1 and knob 7) read
        # the same value; that's expected, not a bug. What matters is knobs aren't in
        # lockstep: adjacent knobs read different values at the same instant.
        self.assertTrue(all(values[i] != values[i + 1] for i in range(7)))

    def test_native_parse_error_guard_filters_by_session(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as stream:
            path = stream.name
            stream.write("old\n")
            cursor = stream.tell()
            stream.write("Arguments cannot be parsed: [other-session]\n")
        try:
            self.assertGreater(probe.assert_no_session_parse_errors("wanted", cursor, path), cursor)
            with open(path, "a", encoding="utf-8") as stream:
                cursor = stream.tell()
                stream.write("Arguments cannot be parsed: [wanted]\n")
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                probe.assert_no_session_parse_errors("wanted", cursor, path)
        finally:
            os.unlink(path)

    def test_unique_stream_frames_are_stable_unique_and_bounded(self):
        frames = [probe.webp_stream_frame(index, 20) for index in range(20)]
        self.assertEqual(20, len({hashlib.sha256(frame).digest() for frame in frames}))
        self.assertEqual(frames[7], probe.webp_stream_frame(7, 20))
        self.assertLess(sum(map(len, frames)), 4 * 1024 * 1024)

    def test_unique_stream_frame_rejects_invalid_sequence(self):
        with self.assertRaises(ValueError):
            probe.webp_stream_frame(0, 1)
        with self.assertRaises(ValueError):
            probe.webp_stream_frame(20, 20)

    def test_animated_stream_is_multiframe_and_has_requested_timing(self):
        from PIL import Image

        data = probe.webp_animated_stream(count=4, fps=25, width=160, height=53)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertTrue(image.is_animated)
            self.assertEqual(image.n_frames, 4)
            self.assertEqual(image.info["loop"], 0)
        offsets = [index for index in range(len(data)) if data.startswith(b"ANMF", index)]
        self.assertEqual(len(offsets), 4)
        durations = [int.from_bytes(data[offset + 20:offset + 23], "little") for offset in offsets]
        self.assertEqual(durations, [40] * 4)

    def test_animated_stream_rejects_invalid_bounds(self):
        with self.assertRaises(ValueError):
            probe.webp_animated_stream(count=1)
        with self.assertRaises(ValueError):
            probe.webp_animated_stream(fps=61)

    def test_capture_inspector(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            path = f.name
            f.write(json.dumps({"kind": "frame", "method_name": "add_asset", "message": [0, 1, 3, []]}) + "\n")
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as output:
                output_path = output.name
            result = probe.inspect_capture(path, output_path)
            self.assertEqual(result["method_counts"]["add_asset"], 1)
            self.assertEqual(len(result["display_events"]), 1)
        finally:
            os.unlink(path)
            os.unlink(output_path)

    def test_names_dictionary_keys_but_not_integer_values(self):
        registry = ["zero", "field_one"]
        got = probe.name_dictionary_keys({"1": 1, "99": [{1: 7}]}, registry)
        self.assertEqual(got, {"field_one": 1, "99": [{"field_one": 7}]})

    def test_settings_summary_bounds_opaque_image_data(self):
        data = b"RIFF" + b"x" * 200
        got = probe.collect_setting_fields({
            "template_name": "Test",
            "pages": [{"image_data": data}],
            "unrelated": "ignored",
        }, {"template_name", "pages", "image_data"})
        self.assertEqual([item["path"] for item in got], [
            "settings.template_name",
            "settings.pages",
            "settings.pages[0].image_data",
        ])
        image = got[-1]["value"]
        self.assertEqual(image["type"], "bytes")
        self.assertEqual(image["length"], len(data))
        self.assertEqual(image["prefix_hex"], data[:32].hex())
        self.assertNotIn(data.decode("ascii"), json.dumps(got))

    def test_active_template_image_changes_only_image_data(self):
        registry = ["unused", "active_midi_template", "midi_templates", "template_name", "image_data"]
        settings = {
            1: "Active",
            2: [
                {3: "Other", 4: "old-other", "keep": [1, 2]},
                {3: "Active", 4: "old-active", "keep": {"x": True}},
            ],
            "global": 7,
        }
        updated, name, original = probe.set_active_template_image(settings, registry, "new-image")
        self.assertEqual(name, "Active")
        self.assertEqual(original, "old-active")
        self.assertEqual(updated[2][0][4], "old-other")
        self.assertEqual(updated[2][1][4], "new-image")
        self.assertEqual(settings[2][1][4], "old-active")
        updated[2][1]["keep"]["x"] = False
        self.assertTrue(settings[2][1]["keep"]["x"])

    def test_importable_artwork_template_is_a_separate_copy(self):
        registry = ["unused", "active_midi_template", "midi_templates", "template_name",
                    "image_data", "hide_template_name"]
        settings = {
            1: "Default",
            2: [{3: "Default", 4: "", 5: False, "pages": [{"name": "Page 1"}]}],
        }
        got = probe.build_midi_template_artwork(settings, registry, "data:image/webp;base64,abc")
        self.assertEqual(got["template_name"], "KompleteSynthesia Image Test")
        self.assertEqual(got["image_data"], "data:image/webp;base64,abc")
        self.assertTrue(got["hide_template_name"])
        got["pages"][0]["name"] = "Changed"
        self.assertEqual(settings[2][0]["pages"][0]["name"], "Page 1")

    def test_midi_template_image_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "--confirmed"):
            probe.run_midi_template_image(confirmed=False, path="not-used")

    def test_settings_restore_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "--confirmed"):
            probe.restore_settings_backup("not-used", confirmed=False, path="not-used")

    def test_schema_substitution_resolves_symbols_and_binary(self):
        class FakeSession:
            uuid = "U"
            def resolve(self, name):
                return {"add_asset": 9}[name]
        data = b"abc"
        got = probe.substitute(["$uuid", "$symbol:add_asset", "$asset"], {"asset": data}, FakeSession())
        self.assertEqual(got, ["U", 9, data])

    def test_schema_includes_targeted_plugin_data_background_update(self):
        schema = probe.load_schema(os.path.join(os.path.dirname(__file__), "fixtures",
                                                "odr_display_schema_2_2_0.json"))
        targeted = schema["show_asset_plugin_data"]
        self.assertEqual(targeted["method"], "$symbol:client_parameter_page_set_plugin_data")
        self.assertEqual(targeted["params"][2]["$symbol:background"], "$asset_sha256")

    def test_schema_includes_localized_browser_image_update(self):
        schema = probe.load_schema(os.path.join(os.path.dirname(__file__), "fixtures",
                                                "odr_display_schema_2_2_0.json"))
        update = schema["update_browser_asset"]
        self.assertEqual(update["method"], "$symbol:client_browser_set_sound_item")
        self.assertEqual(update["params"][2], 0)
        self.assertEqual(update["params"][3]["$symbol:asset_image"], "$asset_sha256")

    def test_schema_includes_rendered_product_thumbnail(self):
        schema = probe.load_schema(os.path.join(os.path.dirname(__file__), "fixtures",
                                                "odr_display_schema_2_2_0.json"))
        product = schema["show_browser_product"]
        self.assertEqual(product["method"], "$symbol:client_browser_set_filter")
        self.assertEqual(product["params"][2], "products")
        item = product["params"][3]["$symbol:items"][0]
        self.assertEqual(item["$symbol:thumbnail_image"], "$asset_sha256")


if __name__ == "__main__":
    unittest.main()
