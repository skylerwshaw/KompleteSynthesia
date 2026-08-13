# TODO

Most items below are MK3 gaps. For why MK3 support matters and where things stand
overall, see [MK3_COMPATIBILITY.md](MK3_COMPATIBILITY.md) first.

## MK3 control surface: unresponsive after first real key-light write (FIXED)

**Fixed** by lighting the keys through NI's hardware connection service instead of driving
the device ourselves, which never enters the legacy LED mode that caused this. Verified on
an S88 MK3: keys light as they are played and the jogwheel, buttons and knobs keep working.
See [ODR_PROTOCOL.md](ODR_PROTOCOL.md) for the protocol and `ODRClient` for the
implementation. The history below is kept because it records what was ruled out. There is
no legacy fallback: the legacy LED-mode code has been deleted outright, so if NI's service
is not running an MK3 simply gets no light guide for the session, keys stay dark, but the
control surface keeps working, which is the whole point of not touching that mode at all.

The S88 MK3's control surface (buttons/jogwheel/knobs, over the `"DAW"` MIDI port
after the NIHIA handshake in `MIDIController.m`) goes fully unresponsive the first
time a real (non-zero) key gets lit, confirmed reliable, solo, with no Synthesia
song loaded. Only a physical USB unplug/replug recovers. Note velocity has also been
observed stuck at 127 (not re-confirmed against this specific trigger).

**Root cause**: entering the MK3's HID legacy LED mode (`kKompleteKontrolInit`, `A0 00
00`, used to wrap every lightguide write in `HIDController.m`) kills the DAW-port
NIHIA session below the MIDI layer, confirmed via MIDI Monitor (an independent tool
bypassing this app entirely): a button press in the broken state produces nothing at
the OS level, not just nothing in this app's logs. Two MIDI-layer recovery attempts
(resending the NIHIA handshake after every write; also resending the mystery
`06 00 00 00 93 02 cd 01 2c 90` HID packet real Komplete Kontrol sends) were both
tried and confirmed insufficient on real hardware, then removed: a break below the
MIDI layer can't be fixed by anything sent over MIDI. A genuine, unrelated data race
between concurrent lightguide writers (physical key presses vs. Synthesia's
light-loopback port, both hitting `updateLightGuideMap:` unsynchronized) was found and
fixed regardless (`@synchronized(self)` in `HIDController.m`), real bug, kept, but
not the cause of this symptom (doesn't reproduce solo).

**Ruled out: USB contention with Native Instruments' background service.** The
`kIOReturnExclusiveAccess` error that used to block raw USB access came from
`NIHardwareConnectionService`, an ordinary NI 2.x userspace app holding USB interface 3
(`ioreg -l -w0 | rg -A20 "ODR@3" | rg UsbExclusiveOwner`), not from a macOS class driver
as previously assumed. It is now terminated at launch alongside the three older NI
processes `AppDelegate.m` already handled, and interface 3 opens cleanly. That did not
fix this bug: measured on hardware with NI's service dead and this app owning interface
3, the control surface went from 108 CC packets to zero the instant Synthesia lit keys.
Entering legacy LED mode kills it on its own.

**Ruled out via raw USB interface 3, before the ODR fix below was found:** replaying the
mystery `06 00 00 00 93 02 cd 01 2c 90` heartbeat over bulk (the transport it actually
belongs on, decoded as MessagePack `[2, 300, []]`) every 8s, with every transfer confirmed
delivered, did not stop the control surface dying on the first lighting write. Reading the
device's bulk IN endpoint continuously showed it sends nothing back, ever: inputs come
over MIDI, full stop. And activating the MK3's real "PLUG-IN mode" (confirmed to exist by
the third-party [`kontrol-s88-mk3-linux`](https://github.com/HugginsIndustries/kontrol-s88-mk3-linux)
reverse-engineering project) via its MessagePack handshake over bulk, with delivery
confirmed, produced no visible change on the device: the handshake alone doesn't switch
modes, and even if it did, that project's light guide and input protocols are undecoded.

**Solved, by not fighting the device at all: see [ODR_PROTOCOL.md](ODR_PROTOCOL.md).**
`NIHardwareConnectionService` (the process we had been killing to get at USB interface 3)
exposes the whole device as an msgpack-RPC service on a Unix socket, and accepts any local
client. Lighting keys through it works on hardware in per-note colours *while the jogwheel
and knobs keep sending CC*, because nothing in that path enters legacy LED mode. Reference
client and self-check: `scripts/odr_lightguide.py`. Adopting it means keeping NI's service
alive instead of terminating it in `AppDelegate.m`, and the legacy LED path has been
deleted from `HIDController.m` entirely rather than kept as a fallback: it reintroduces
the exact bug above, so there is no safe way to fall back to it.

Raw USB reverse engineering, Wireshark plus usbmon against real Komplete Kontrol in a
Windows VM with USB passthrough, capturing the whole session from plug-in onwards, is now
a fallback rather than the plan, worth revisiting only if the IPC route proves unstable
across agent versions.

Also found and fixed in passing: `USBController.m`'s `kUSBDeviceInterfaceMK3` was
`0x04` (should be `0x03` per the reference project, endpoint `0x04` was already
correct); never caught before because `VideoController`, the only prior caller of
`bulkWriteData:`, is `mk == 2`-only, so this path had never run on real MK3 hardware.

**Diagnosing ODR lighting: quit Komplete Kontrol first, every time.** The service
gives focus to one client at a time, and Komplete Kontrol running in the background
(easy to forget, no window needed, and it silently reconnects on its own) is
indistinguishable from this app's own lighting at a glance: its idle/browse-state
colours and its LCD content are both still live on the device either way. Confirmed
this cost real time during the 2.1.5 method-ID re-capture below, a lit-but-wrong
board and an LCD showing instrument info both turned out to be Komplete Kontrol still
running, not a bug in the app under test. `ps aux | grep -i "Komplete Kontrol"` is the
reliable check; the on-screen dock/window state is not.

**2026-08-12: the "worth revisiting" method-ID drift above actually happened, and is
now fixed.** @Bounga reported lighting silently dead on his S61 after Native Access
auto-updated Hardware Connection Service to 2.1.5 (R14): `connect_device` (`382`),
`client_request_focus` (`373`) and `client_lightguide_set_leds` (`360`) all started
returning `Method not registered` (issue #18, comment 5208665711). Reproduced locally
by updating this machine's own service the same way and confirming the identical
refusal on real hardware.

Root cause: those numbers were never guaranteed stable, they're just each method's
index into the service's `symbol_registry`, an array the hello reply already includes
in full (several hundred names), and the registry itself gets reordered/changed between
agent releases. The names are stable; the numbers derived from them are not. Re-captured
the current names by relaying a real Komplete Kontrol session through
`scripts/odr_relay_capture.py` (moves the service socket aside, binds the original path,
logs both directions), `connect_device` is unchanged, but focus and lighting are now
`client_request_focus` and `client_lightguide_set_leds`, and the LED array's field key
changed from `239` to a differently-named `client_midi_addressing` (a guessed bare
positional array got `Arguments cannot be parsed` from the service; the real traffic
showed the actual dict-wrapped shape).

**Fix, not a patch**: `ODRClient.m` and `odr_lightguide.py` now resolve all four
method/field numbers from `symbol_registry` by name on every connection, instead of
using hardcoded numbers, so this should not require a code change again next time the
service renumbers things, only if it drops one of the names outright (which surfaces as
a loud, specific error rather than silent no-op lighting). Verified end-to-end on real
hardware against the drifted service: both the standalone script and the actual app
(`ODR lighting active for <serial>` in the log, keys visibly lighting on key press).

Also found and fixed in passing while chasing this: `ODRClient.m`'s `awaitReply` kept
only the *last* `recv()` chunk of a multi-chunk reply (`setData:` instead of
`appendData:`), harmless while nothing read past a reply's last 2 bytes, but silently
truncating the hello reply's `symbol_registry`, which sits at the end of a ~10KB body,
the moment something needed to read it.

## MK3: default button/strip/ring lighting goes out permanently on first key press

On boot, the S88 MK3 shows its own default lighting (buttons, jogwheel ring, touch
strip). The moment the app lights the first key, all of that default lighting goes
dark and never comes back, only individually-pressed key LEDs light up afterward.

Likely cause: every key-light update (`updateLightGuideMap:` in `HIDController.m`)
wraps the write in `A0 00 00` (enter "legacy LED mode") / `A0 01 00` (exit), which is
what lets lighting coexist with MIDI (see commit `8fa3fd8`). Working theory: entering
that mode makes the device relinquish its own autonomous default lighting for
anything we're not actively driving ourselves. We only ever write the 88 key-light
bytes in the lightguide buffer; button lighting (`updateButtonLightMap:`) is a
no-op for MK3, and the buffer's touch-strip/wheel-ring region is never populated,
so once control is handed off, those never come back on.

Button/control-surface *lighting* was always out of scope (we only implemented
*input* for buttons/jogwheel/knobs). Options if this gets revisited: populate the
unused tail of the lightguide buffer (untested old hint that it may address touch
strip/wheel/button zones), or implement real button lighting via
`updateButtonLightMap:`.

## Button 1-8 above the display, and BROWSER/PLUG IN/SETUP mode buttons

Not mapped. These appear gated on the device having actual DAW mixer/track/plugin
state to report against (confirmed: pressing them sends nothing even after
switching the device into DAW mode via the physical DAW button, which itself sends
CC 0x05). Unlocking them would mean implementing a slice of the SysEx track-sync
protocol (`SYSEX_TRACK_AVAILABLE`/`SYSEX_TRACK_NAME`/etc. from
`git-moss/DrivenByMoss`'s `KontrolProtocolControlSurface.java`) we've deliberately
not built, to give the device something to populate them with.

## Shift+Play / Shift+Record

NI's manual documents these as distinct functions ("Restart" / "Count-in") from
plain Play/Record. Untested whether the device sends its own distinct CC for the
Shift-combo (like it does for Shift+Undo->Redo, confirmed to be a firmware-level
re-encode, not something we compute from separately-held Shift state) or requires
us to track Shift state ourselves.

## Knob/button IDs that are recognized but dispatch to nothing

Several CCs are correctly decoded and routed to the right `kKompleteKontrolButtonId*`
in `MIDI2HIDController.m` (Metro, Tap Tempo, Undo/Redo, Quantize, Automation,
Previous/Next, Knobs 2-8) but `receivedEvent:value:` has no actual case for them
(pre-existing gap, not introduced by the MK3 work), they're logged/no-op today.
Worth deciding what, if anything, each should actually do.

## Display/screen mirroring for MK3 (ODR banner path verified; integration not started)

The durable research plan for flowing motion, including the four remaining avenues and
their decisive experiments, is in [`MK3_VIDEO_RESEARCH.md`](MK3_VIDEO_RESEARCH.md).
Do not treat ODR's observed cadence as a proven hardware display limit.

`VideoController.m`'s speculative MK3 path (screen size 1280x480, command byte still
literally named `kCommandScreenUpdateMK2`, disabled 32-bit-alignment assertions) must not
be enabled. On 2026-08-12 a guarded probe sent its bare legacy `0x84` format twice as a
single 64x64 RGB565 rectangle. Both 8,228-byte USB transfers completed on MK3 interface
3 endpoint 4, but the S88 MK3 display remained unchanged on “Default MIDI template.”
The direct MK2 compatibility path is therefore closed; investigate pixel or renderer
operations only inside MK3's framed `DISPLAY`/MessagePack transport.

The raw-USB route MK2 uses remains blocked while `NIHardwareConnectionService` owns USB
interface 3, but MK3 no longer needs that route for a useful first display implementation.

**2026-08-12: ODR arbitrary imagery and refresh verified on an S88 MK3.** A real Komplete
Kontrol capture revealed that the parameter page's `plugin_data.background` field refers
to an asset uploaded by SHA-256 through `add_asset`. Two custom lossless WebPs rendered
upright and at the expected colors across the full 1280x212 banner region. Alternating
the two pre-uploaded assets worked visibly at 0.5, 2, 5, and 10 requested FPS; the 1 FPS
stage was sent but not operator-scored. At 2 FPS there may have been a very short dark
interval between frames, but it was not certain and 5/10 FPS looked clean. The registered
asset inventory was 100 before the ladder and remained 100 after the probe disconnected
and the inventory client reconnected, so identical hashes are deduplicated. Full protocol
details and timings are in `ODR_PROTOCOL.md`.

This proves a useful partial-height Synthesia display is possible, but app integration is
still open:

**2026-08-12: native on-device renderer animation confirmed smooth at 30 Hz, for one knob
and for all eight at once.** Per `MK3_VIDEO_RESEARCH.md` avenue 3, driving device-native
knob values directly (no image upload, no asset swap) animates fluidly on real hardware.
First a single knob ("super smooth," operator's words), then all eight simultaneously,
phase-staggered, 240 messages/second combined, also confirmed independent and smooth with
no degradation, then all eight again at 60 Hz (480 messages/second combined), still
smooth. This is the first result that beats the WebP asset-swap ceiling above.

Also checked directly on hardware: whether any real NKS2 instrument (Massive X, Hypha,
both owned) drives continuous animation on the MK3 screen itself, not just in Komplete
Kontrol's own host-side plugin GUI. First check (Massive X's wavetable knob) looked
negative, but a relay capture caught the real story: Hypha streams its Morpher X/Y
position continuously regardless of what's on screen, and it's just sent to a knob page
(Performance) the operator wasn't looking at. Navigating to the actual Morpher page
confirmed it animates, real-world precedent for exactly the mechanism this project's own
probe already uses. Doesn't reveal a richer widget type, still just knobs, so it narrows
rather than proves whether enough independent objects exist to represent falling notes,
see avenue 3's "Next work" in `MK3_VIDEO_RESEARCH.md`, which is now mostly "how much of
the 1280x480 panel can knobs/objects tile across."

- Capture and crop/scale the Synthesia view to 1280x212, encode WebP frames, and feed the
  ODR asset/background path at a conservative rate no higher than the verified 10 FPS.
- Reuse one ODR session for lighting and display ownership; coordinate focus, page
  selection, reconnect, and restoration so controls and the light guide stay functional.
- Bound or recycle content hashes. Video frames are normally unique, unlike the two test
  assets, so long-running playback must not grow the service's persistent asset inventory.
- Measure end-to-end capture-to-display latency and investigate the possible dark interval
  with a camera rather than relying on visual memory.
- Determine whether another model slot can cover more than the verified 1280x212 banner.
  No full 1280x480 framebuffer path has been found.

## Feature disparity (MK1/MK2 vs MK3)

Everything here is either a live regression from what MK1/MK2 already do, or a new door
MK3 opens that neither generation has today. Where a gap already has its own section
above with more detail, this points there instead of repeating it.

### MK1/MK2 has it, MK3 doesn't

- **Full-screen mirroring.** See "Display/screen mirroring for MK3" above. Arbitrary
  1280x212 imagery is verified through ODR, but capture/encoding and app integration are
  not implemented and no full 1280x480 path is known.
- **All button LED feedback, including the default lighting MK3 shows on boot.**
  `HIDController.m`'s `updateButtonLightMap:` is a no-op for MK3 (`// FIXME: We dont know
  yet how to specifically update the button lighting.`), and the momentary flash-on-press
  feedback is only ever invoked from the MK1/2 HID report handler, structurally
  unreachable for MK3. This is why the default lighting never comes back once a key is
  lit, see "MK3: default button/strip/ring lighting goes out permanently" above.
- **Setup, Clear, Scene, Function1-5, and Plugin buttons are completely unreachable on
  MK3**, not merely no-op, no CC is mapped to them at all in
  `MIDI2HIDController.m`'s `receivedMK3ControlSurfaceCC:`. On MK1/2 these open
  Preferences, reset the app, bootstrap Synthesia, toggle screen mirroring, and fire
  Escape/F2/F3/F4 shortcuts, respectively. Related to, but broader than, "Button 1-8
  above the display" above.
- **The volume knob (Knob1).** MK1/2 have a dedicated physical volume knob bound to
  system volume and the on-screen OSD. MK3's Knob1 is just the leftmost of 8 generic,
  context-dependent soft knobs, deliberately left unmapped in `MIDI2HIDController.m`
  rather than repurposed, turning it does nothing.
- **Ten more CCs decoded and routed but dispatched to nothing**: Record, Stop, Loop,
  Metro, Tap Tempo, Undo/Redo, Quantize, Automation, Preset Up/Down, Knobs 2-8. See
  "Knob/button IDs that are recognized but dispatch to nothing" above for the CC table.

### Different mechanism, not a gap

Key lighting (ODR socket vs. direct HID write), button/jogwheel/knob input (NIHIA MIDI
port vs. vendor HID), jogwheel scroll, and jog-tilt decoding are all functionally present
on MK3, just wired differently than MK1/2. Not tracked as gaps.

### New potential functionality (neither generation has this today)

- **DAW-mode SysEx track sync** (`SYSEX_TRACK_AVAILABLE`/`SYSEX_TRACK_NAME` etc., per
  `DrivenByMoss`'s `KontrolProtocolControlSurface.java`). Appears to be what gates
  Buttons 1-8 above the display and the Browser/Plug-in/Setup modes, per "Button 1-8
  above the display" above, genuinely new work, not present on MK1/2 either.
- **Driving button/ring/strip lighting ourselves**, via `updateButtonLightMap:`
  (currently a stub for MK3) or the lightguide buffer's unused tail bytes, instead of
  relying on the device's own default lighting.
- **MK3's larger screen** (1280x480, one panel), bigger than anything this app has
  driven before (MK2 is two 480x272 panels), once mirroring is wired up at all.

## Wanted: an S49 MK3 owner, and anyone with an S61 MK3

Everything MK3 here has been developed and verified against exactly one keyboard, an S88
MK3. Two gaps need hardware nobody working on this has:

- **S49 MK3 is unsupported.** Its product ID (`kPID_S49MK3 = 0x2100` in `USBController.h`)
  is confirmed, not a guess, tillt pulled it straight out of the firmware image via static
  analysis (discussion #29, comment 9458947), but it is still missing from
  `HIDController.m`'s device table entirely, so the app will not recognise one whatever the
  lighting path. Nobody has actually run this app, or `scripts/odr_lightguide.py`, against
  real S49 MK3 hardware yet, that's the one report still needed.
- **S61 MK3 confirmed working, by @Bounga (issue #18, comment 5208383973).** Product ID
  `0x2110`, in the table already. Phase 1 lit every key, phase 2 spanned the full 61-key
  range (MIDI 36..96) with no offset, this was on Hardware Connection Service 2.0.7,
  before the method-ID drift above; unaffected now that the fix resolves names instead
  of numbers, but worth a re-confirm from an S61 owner if anyone doubts that.

Neither is a code problem waiting on a decision; both are waiting on one report from
someone with the hardware.

### The test protocol, in full

```
./scripts/odr_lightguide.py
```

That is all of it. Roughly 20 seconds, needs `pip install msgpack`, and needs Native
Instruments' hardware connection service running, which it is by default, quit Komplete
Kontrol first so it is not holding focus. The script works on keyboards this app cannot yet
recognise, because it asks the service what is attached instead of consulting our own
product-ID table, and it addresses all 128 MIDI notes so it assumes no particular key
count. It only lights keys; nothing is written to the device and nothing is left changed.

Three things to report back:

1. The device line it prints, **product ID especially**, that is the missing datum.
2. Whether phase 1 lit every key on the keyboard.
3. The lowest and highest key lit in phase 2, by name, e.g. `C2` and `C6`.

What each answer buys: (1) goes straight into `ProductID` in `USBController.h` and the
device table in `HIDController.m`. (3) gives the `offset` for that table entry: it is the
negated MIDI note of the lowest key, so a lowest lit `C2` means `-36`. A 49-key is expected
to run C2..C6 and a 61-key C2..C7, so (3) mostly confirms the guess. If (2) shows keys that
never light, the LED array is not a plain MIDI-note map on that model and
[ODR_PROTOCOL.md](ODR_PROTOCOL.md) needs revisiting.

With those three answers the table entry is a two-line change, no hardware needed at this
end. Note that this covers lighting only: the NIHIA control-surface handshake would still
be unverified on those models.
