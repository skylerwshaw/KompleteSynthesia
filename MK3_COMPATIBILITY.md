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
| 2. LED guidance per key | Mostly works, keys light up live as you play. Regression: the device's own default lighting (buttons, jogwheel ring, touch strip) goes dark permanently the moment the first key is lit, and never comes back (see `TODO.md`). |
| 3. Controls | **Broken for real use.** Buttons/jogwheel/knobs work fine right after boot, until the very first real key gets lit. From that point on, all control-surface input goes permanently silent until a physical USB unplug/replug. Since pillar 2 is also a goal, and lighting a key is literally the first thing that happens when someone starts playing, pillars 2 and 3 are currently mutually exclusive in practice: get lighting, lose controls. |

That conflict (not a small bug, an architectural incompatibility between how this
app currently does lighting and how it currently does control-surface input) is why
this has taken sustained investigation rather than a quick patch. See `TODO.md`'s top
section for the full root-cause trail.

## Why this is hard

The short version (`TODO.md` has the full evidence trail): this app currently gets
pillar 3 (controls) via a workaround: pretending to
be a DAW and using a Bitwig-remote-control MIDI protocol (`DrivenByMoss`/NIHIA) that
was never designed for this use case, layered on top of pillar 2 (lighting) via a
separate, older HID compatibility scheme (`A0 00 00` "legacy LED mode"). Confirmed via
live testing (MIDI Monitor, bypassing this app entirely) that entering that legacy LED
mode to light a key kills the control-surface session at a level below MIDI: no
software-side recovery trick sent over MIDI can fix it, because the break isn't in
MIDI at all.

The obvious candidate for a real fix is to stop combining two hacks and use the
keyboard's actual native mode instead. A third-party reverse-engineering project
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

- **`TODO.md`**: the full list of open MK3 items (this conflict, plus six smaller,
  independent gaps: default lighting loss, unmapped buttons, Shift+Play/Record,
  dead knob/button IDs, screen mirroring, a missing product ID). Read the top section
  for the complete root-cause investigation trail behind this doc's summary.
