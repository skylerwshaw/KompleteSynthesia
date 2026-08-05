# The ODR protocol: lighting MK3 keys without the legacy LED mode

Native Instruments' `NIHardwareConnectionService` ("ODR agent") owns the S-series MK3 over
USB interface 3 and exposes the whole device (key LEDs, screens, mixer, browser) as an
msgpack-RPC service on a Unix domain socket. Komplete Kontrol is just a client of it.

**We can be a client too.** Verified end to end on an S88 MK3: keys light in per-note
colours, and the control surface (jogwheel, buttons, knobs) keeps working the entire time,
because nothing in this path touches the legacy HID LED mode that breaks it.

Reference implementation and runnable check: [`scripts/odr_lightguide.py`](scripts/odr_lightguide.py).
`--selftest` verifies our encoder byte-for-byte against a capture of Komplete Kontrol's own
handshake and needs no hardware.

## Transport

Unix stream socket at:

    /Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks

A connection opens with **the client's UUID as 16 raw bytes**, sent bare, ahead of and
outside the framing. Everything after that is a `uint32` little-endian length followed by an
msgpack-RPC message (`[0, msgid, method, params]` request, `[1, msgid, error, result]`
reply, `[2, method, params]` notification).

That preamble is the single thing that gates entry. Without it the service accepts the
connection, reads the traffic and answers nothing at all, no error, no close. Every
earlier probe failed on exactly this and looked like wrong framing or a wrong method name.
It is also why a naive relay sees nothing in the client-to-server direction: read those 16
bytes as a length prefix and the stream is misaligned forever.

There is no code-signature or entitlement check on the peer. Any local process can connect.

## Method numbering

`instance_hello` is sent as a string. After that both sides address methods as **integers**
from the service's symbol registry, and structure fields are integers too (`{239: [...]}`
rather than `{"leds": [...]}`). The numbers below are what an S88 MK3 on agent 2.0.7 (R15),
IPC protocol 2.1.0, was observed using. They are not guaranteed stable across agent versions.

| Number | Meaning | Kind |
|---|---|---|
| `"instance_hello"` | handshake, the only string-named method | request |
| `382` | connect/attach to a device | request, replies `True` |
| `373` | request focus | notification |
| `360` | set key LEDs | notification |
| `239` | the LED array field inside `360` | field |
| `345`, `348`, `350` | device attached / settings changed / focus granted | notifications from the service |

Calling `360` as a request rather than a notification returns
`Method not registered: {}`, handlers are registered per message kind.

## Bringing up a session

```
c->s  <16 raw UUID bytes>
c->s  [0, 0, "instance_hello", [uuid, {client_info: {name, version, type: "standalone"},
                                       ipc_protocol_version: "2.1.0"}]]
s->c  [1, 0, None, {agent_version, ipc_protocol_version, available_devices: [...], ...}]
c->s  [0, 1, 382, [uuid, serial]]        -> [1, 1, None, True]
c->s  [2, 373, [uuid, serial]]           # focus
c->s  [2, 360, [uuid, serial, {239: [[note, colour], ...]}]]
```

The hello reply enumerates devices with product name, serial, vendor and product ID, take
the serial from there; every later call is addressed by it.

Attaching answers `[1, msgid, nil, true]`, and a serial the service does not recognise gets
`[1, msgid, nil, false]` rather than an error, worth checking, since carrying on from a
refusal means every later LED message is accepted and ignored.

**Focus is required.** Without the `373` notification the service accepts `360` silently and
nothing on the keyboard changes. This was the second dead end: LED messages structurally
identical to Komplete Kontrol's, ignored, while stale colours from the last real KK session
stayed latched on the device and looked like our own output.

## The LED array

`{239: [[index, colour], ...]}` with **128 entries, indexed by MIDI note number**, not by
physical key. An S88 occupies MIDI 21 (A0) through 108 (C8); smaller keyboards take a
sub-range. Sending 88 entries as `0..87` lights A0 up to D#6 and leaves the top 21 keys
dark, which is a convincing-looking wrong answer.

Because the array is always the full MIDI range, it is the same message for every keyboard
in the range, a 61-key S-series just occupies MIDI 36..96 and leaves the rest at zero.
Nothing here is specific to one model or one unit: the device is addressed by the USB serial
the keyboard itself reports, which `HIDController` reads off the HID device
(`kIOHIDSerialNumberKey`), and the key count and starting note come from its existing
per-product table. The hello reply also enumerates devices with their serials, which is how
the Python client finds one without IOKit.

The colour byte is the same encoding the legacy HID path already uses (`kKompleteKontrolColor*`
in `HIDController.h`): palette index in the high six bits, intensity in the low two.
`0x00` is off. Intensity `3` renders dark on this device, so `colour | 0x03` blanks a key
rather than brightening it, worth knowing, because a whole board set to `|3` looks
exactly like a rejected message.

Each `360` replaces the entire array; there is no per-key delta.

## Why this matters

The long-standing MK3 bug is that entering legacy HID LED mode (`A0 00 00`) kills the DAW-port
NIHIA session below the MIDI layer, so lighting keys costs you the control surface until a
physical replug. See [TODO.md](TODO.md) for everything that ruled out:
keepalives on the right transport, killing NI's service to take USB interface 3 directly,
the PLUG-IN mode handshake.

This path sidesteps the conflict entirely rather than fixing it: NI's service keeps
ownership of the device and drives it the way Komplete Kontrol does, and we ask it for
lighting. Confirmed on hardware: keys lit in five distinct colours while the knobs kept
sending CC.

## How the app uses it

`ODRClient` implements the above; `HIDController` connects it in `setupWithError:` when an
MK3 is found, and `updateLightGuideMap:` routes the key map through it. There is no legacy
LED path left to fall back to: it has been deleted from `HIDController.m` entirely,
because it is the very thing that kills the control surface. If the service is not running,
or the connection otherwise fails, `updateLightGuideMap:` just returns without writing
anything: the MK3 goes without a light guide for the session, and the control surface keeps
working. `-mk3_odr_lighting NO` forces the same no-lighting outcome, for testing.

There is one startup wrinkle. `AppDelegate.m` terminates NI's background processes at
launch, and the connection service was among them precisely because MK2 screen mirroring
needs the USB interface it holds. An MK3 needs the opposite (the service alive), so it is
now spared when `+[HIDController mk3DeviceAttached]` says an MK3 is plugged in, and killed
exactly as before otherwise.

The cost is a hard dependency on `NIHardwareConnectionService` running, and on method
numbers that may shift between agent releases.

## Capturing more of it

The service does report errors for malformed requests
(`Arguments cannot be parsed: [...]`, `Method not registered: {}`), so replies are a usable
oracle, but only for requests, not notifications, which covers most of the interesting
surface. To learn a new message, watch Komplete Kontrol perform the action through a relay
that moves the socket aside, binds the original path, and forwards to the real one.
Remember the 16-byte preamble when parsing the client side.

Beyond lighting, the same session carries the screens, mixer track data, browser model,
plugin chain and parameter pages, the full PLUG-IN mode feature set that raw USB bulk
never yielded, already decoded and served by the agent.
