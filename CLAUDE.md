# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A native macOS menu-bar app that routes [Synthesia](https://synthesia.com) piano
lighting to Native Instruments Komplete Kontrol S-series keyboards (MK1/MK2/MK3) and
mirrors the Synthesia window onto the MK2 displays. Objective-C, AppKit, **no third-party
dependencies** (deliberate, see README "Background and Motivation"). The `scripts/` dir
holds a separate Python toolkit for MK3 protocol work.

## Build / run / test

- **App**: open `KompleteSynthesia.xcodeproj` in **full Xcode** (Command Line Tools alone
  can't build it; `xcodebuild` will error under CLT). Scheme `KompleteSynthesia`. No package
  manager, no dependency fetch step.
- **DMG packaging**: `scripts/build_dmg.sh`, run from the directory containing the built
  `KompleteSynthesia.app`. Needs `create-dmg` on PATH.
- **Python tooling** needs `msgpack` (only non-stdlib import). Reference ODR implementation
  and its encoding self-check: `python3 scripts/odr_lightguide.py --selftest`.
- **Python tests** (unittest, no framework): `python3 scripts/test_odr_display_tools.py`
  and `python3 scripts/test_mk3_usb_capture_decode.py`. Run a single case with the standard
  `... TestClass.test_method` unittest selector.

Running the app requires macOS **Input Monitoring** (button events), **Screen Recording**
(MK2 window mirror), and **Accessibility** (synthetic play/stop key events) permissions.

## Architecture

Data flows: Synthesia lights notes on a virtual MIDI port → app maps notes to per-key
colors → pushes colors to the keyboard over the path that fits the device generation.

- **`MIDI2HIDController`** is the hub. It implements `MIDIControllerDelegate` +
  `HIDControllerDelegate`, owns the `colors` buffer (one byte per physical key,
  `kKompleteKontrolColor* | intensity`), and maps incoming notes to `ColorMapState`
  (unpressed / pressed / left / right hand, etc.).
- **`MIDIController`** listens on the `LoopBe` virtual MIDI input (created during setup);
  Synthesia sends note-on/off there. **`SynthesiaController`** watches and drives the
  Synthesia app itself (parses its XML config/state); **`VirtualEvent`** injects synthetic
  keyboard events for play/stop.
- **`USBController`** detects the keyboard by NI vendor ID + per-model product IDs
  (`kPID_S*MK1/2/3` enums). **`HIDController`** is the legacy LED path.

Two distinct lighting paths, do not mix them:

- **MK1/MK2**: legacy HID LED writes (`HIDController`) plus, for MK2, screen mirroring via
  **`VideoController`** pushing `NIImage` (RGB565) frames over raw USB (`USBController`).
- **MK3**: lighting goes through **`ODRClient`**, which talks to Native Instruments'
  background `NIHardwareConnectionService` over a Unix-socket msgpack-RPC protocol (the app
  is just another client of it, like Komplete Kontrol). **Never drive the MK3 via the legacy
  HID `A0 00 00` LED mode**: it kills the DAW-port NIHIA session and breaks the control
  surface. That is the whole reason the ODR path exists.

## MK3 / ODR specifics

- Protocol, RPC method numbers, and the two silent-failure traps are in **`ODR_PROTOCOL.md`**.
  `scripts/odr_lightguide.py` is the reference implementation (its `--selftest` checks the
  same handshake encoding `ODRClient` builds).
- **ODR method numbers drift between NI service releases.** Resolve them by *name* from the
  service's symbol registry, never hardcode a number.
- MK3 status, gaps, and product-ID coverage: **`MK3_COMPATIBILITY.md`**. Ongoing research
  into fluid MK3 screen video: **`MK3_VIDEO_RESEARCH.md`**.
- Anything that drives ODR against a connected keyboard (lighting, display probes, capture
  scripts) touches real hardware and visibly lights the device. Confirm with the operator
  before running such a probe; firmware-level work is higher-risk still and static-only.

## Setup docs

`SETUP.md` covers the required `LoopBe` virtual MIDI port and the Synthesia "Finger-based
channel" lighting configuration the note-to-color mapping depends on.
