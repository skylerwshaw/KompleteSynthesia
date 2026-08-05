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

**Ruled out: the ~8s heartbeat, this time on the right transport.** The mystery
`06 00 00 00 93 02 cd 01 2c 90` packet decodes as MessagePack `[2, 300, []]` in exactly
the framing `MK3Protocol.m` already builds, so it belongs on interface 3 bulk, the one
transport the earlier HID and CoreMIDI attempts never tried. Sent every 8s over bulk with
every transfer confirmed delivered, the control surface still died on the first lighting
write (31 CC packets, then zero). `MK3MakeKeepAlivePacket()` is kept, but only runs
alongside the PLUG-IN handshake, the context it was captured in.

**Ruled out: the device answering on bulk.** `startBulkReadOnEndpoint:handler:error:` in
`USBController.m` reads endpoint `0x83` continuously (`-mk3_bulk_read YES`); no capture in
any reverse-engineering effort had ever looked at device-to-host traffic here. The device
sends nothing: zero packets while idle, and zero while working the jogwheel and buttons
hard enough to produce 393 CC packets over MIDI. Inputs come over MIDI, full stop.

**PLUG-IN mode: handshake confirmed delivered, device ignores it.** The S88 MK3's actual
"PLUG-IN mode" is confirmed by a third-party reverse-engineering project
([`kontrol-s88-mk3-linux`](https://github.com/HugginsIndustries/kontrol-s88-mk3-linux),
Wireshark captures of real Komplete Kontrol), activated via a MessagePack-encoded
handshake over raw USB bulk transfer. `KompleteSynthesia/MK3Protocol.h`/`.m` implement
that handshake (verified byte-for-byte against the Python reference) and it can be sent
with `-mk3_plugin_mode_probe YES`. The earlier inconclusive run is resolved: all three
packets now report `delivered` in ~125ms rather than burning 1s timeouts, and the device
still shows no visible change, no reply on bulk in, and unchanged MIDI behaviour. So the
handshake alone is not what switches modes, something else in real Komplete Kontrol's
startup is missing, plausibly a control transfer or an alternate interface setting that no
capture has looked at. Note the ceiling even if that is found: the reference project's
light guide support is *planned*, its LIGHTS message is undecoded, and it never reads from
the device, so PLUG-IN mode's lighting and input protocols would still need capturing from
scratch. **Details in `DRIVERKIT_INVESTIGATION.md`**, read it before attempting this
again.

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

## Display/screen mirroring for MK3

Not started at all. `VideoController.m`'s entire MK3 code path (screen size
1280x480, command byte still literally named `kCommandScreenUpdateMK2`, disabled
32-bit-alignment assertions) is untested guesswork never exercised against real
hardware, and `AppDelegate.m` hard-gates `VideoController` instantiation to
`mk == 2` only, so MK3 never reaches it today. No community prior art exists for
this at all, would be a from-scratch effort.

## kPID_S49MK3 missing from the HID-level PID table

Pre-existing gap (`HIDController.m`), unrelated to the MK3 lighting/input work,
unverifiable without S49 hardware.
