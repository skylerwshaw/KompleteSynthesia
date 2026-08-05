# TODO

Most items below are MK3 gaps. For why MK3 support matters and where things stand
overall, see [MK3_COMPATIBILITY.md](MK3_COMPATIBILITY.md) first.

## MK3 control surface: unresponsive after first real key-light write (blocked on macOS driver access, see DRIVERKIT_INVESTIGATION.md)

The S88 MK3's control surface (buttons/jogwheel/knobs, delivered over the `"DAW"` MIDI
port after the NIHIA handshake in `MIDIController.m`) periodically goes fully
unresponsive during a session. Separately, keybed note velocity has also been observed
stuck at 127. Both symptoms only recovered after a physical USB unplug/replug of the
keyboard, suggesting the same underlying NIHIA session dying rather than two unrelated
bugs.

Suspect this needs a periodic keepalive we're not currently sending. Real Komplete
Kontrol software sends a ~10-byte packet to the device every ~8 seconds:

```
06 00 00 00 93 02 cd 01 2c 90
```

This was previously assumed (in earlier, pre-handshake-discovery attempts, see git
history and GitHub discussion #29) to be unnecessary MK3 init noise and discarded.
Investigate whether replicating it on a timer, once the handshake in
`MIDIController.m` completes, keeps the NIHIA session alive instead of degrading over
time.

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
