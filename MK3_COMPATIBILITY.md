# MK3 compatibility: mission and status

## Who this is for

Start here if you're a fresh agent (or human) picking up MK3 work on this repo with no
prior context. This is the "why" doc: for the current blocking technical problem and
its history, see `TODO.md` (the full list of open MK3 items).

## The mission

Bring the Komplete Kontrol S-series **MK3** up to the same level of support MK1/MK2
already have: a Synthesia session where, on an MK3 keyboard, someone can

1. **Play**: physical key presses register as MIDI notes, same as any class-compliant
   keyboard.
2. **Get LED guidance per key**: Synthesia's "which key to press next" lighting shows
   up live on the keybed as they play.
3. **Use the controls**: buttons, jogwheel, and knobs work for transport (play/stop),
   scrubbing, and volume, the same convenience MK1/MK2 users already have.

All three need to work *at the same time, indefinitely, for a whole session*, not
just at boot, not just one at a time.

## Current status

| Pillar | Status |
|---|---|
| 1. Play | Works. Unaffected by anything below. |
| 2. LED guidance per key | Works. Keys light up live as you play, in per-key colours. |
| 3. Controls | Works, *together with* lighting. |

**The pillar 2 vs. pillar 3 conflict is resolved.** Lighting no longer goes through the
legacy HID LED mode that killed the control surface; the keys are lit by asking Native
Instruments' own hardware connection service to do it, over the msgpack-RPC socket
documented in [ODR_PROTOCOL.md](ODR_PROTOCOL.md). Verified on an S88 MK3: keys light as
they are played while the jogwheel, buttons and knobs keep working.

This carries a dependency rather than a workaround: `NIHardwareConnectionService` must be
running, and the service's method numbers may shift between agent releases. There is no
legacy fallback: that path has been removed entirely because it is the bug. When the
service is not available, an MK3 simply gets no light guide for the session; the control
surface keeps working either way.

## Why this was hard

The short version (`TODO.md` has the full evidence trail): this app gets pillar 3
(controls) via a workaround, pretending to be a DAW and using a Bitwig-remote-control
MIDI protocol (`DrivenByMoss`/NIHIA) that
was never designed for this use case, layered on top of pillar 2 (lighting) via a
separate, older HID compatibility scheme (`A0 00 00` "legacy LED mode"). Confirmed via
live testing (MIDI Monitor, bypassing this app entirely) that entering that legacy LED
mode to light a key kills the control-surface session at a level below MIDI: no
software-side recovery trick sent over MIDI can fix it, because the break isn't in
MIDI at all.

The fix came from a third direction: not fighting the device for control of it, but
becoming a client of the service that already owns it, exactly as Komplete Kontrol is.
The rest of this section describes the native-USB route that was pursued first and is now
a fallback plan rather than the plan.

A third-party reverse-engineering project
([`kontrol-s88-mk3-linux`](https://github.com/HugginsIndustries/kontrol-s88-mk3-linux))
confirmed the MK3 has a "PLUG-IN mode" that real Komplete Kontrol activates over raw USB
bulk transfer, a single, unified channel. This app has a verified-correct
implementation of that mode's activation handshake, and can now send it.

Two things stand in the way, and neither is a macOS driver problem:

1. **PLUG-IN mode's useful half is undecoded.** That project implements exactly one
   feature, arpeggiator tempo sync. Its light guide support is listed as *planned*, its
   LIGHTS message is explicitly not decoded, and its code never reads from the device at
   all, so how buttons and knobs report back in PLUG-IN mode is unknown to everyone.
   Getting there requires Wireshark captures of real Komplete Kontrol, the same way that
   project got tempo.
2. **Entering legacy LED mode kills the control surface by itself.** This was previously
   blamed on USB contention with Native Instruments' background service. That has now
   been ruled out by measurement: with NI's service stopped and this app owning USB
   interface 3, the control surface still went from 108 CC packets to zero the moment
   Synthesia lit keys.

The USB access problem that used to be described here as needing a DriverKit system
extension turned out to be Native Instruments' own `NIHardwareConnectionService`
holding the interface, and is fixed, see `TODO.md`.

## Where to go next

- **`TODO.md`**: the full list of open MK3 items (default lighting loss, unmapped
  buttons, Shift+Play/Record, dead knob/button IDs), plus the one item that needs
  hardware rather than work: **all of this was built against a single S88 MK3**, so an
  S49 owner is wanted and an S61 is unverified. That section carries the whole test
  protocol, which is one command. For the screen, arbitrary 1280x212 ODR imagery is now
  verified through 10 requested FPS; Synthesia capture/encoding, focus lifecycle, cache
  bounds, and app integration remain open, and no full 1280x480 path is known. See
  `TODO.md` and `ODR_PROTOCOL.md` for the measured results.
- **`ODR_PROTOCOL.md`**: the service's socket protocol, how lighting actually works now,
  and the two silent-failure traps that make it look unreachable. `scripts/odr_lightguide.py`
  is a standalone reference client.
