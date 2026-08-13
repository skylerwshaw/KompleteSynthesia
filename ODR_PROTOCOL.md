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

`instance_hello` is sent as a string. After that both sides address methods as
**integers**, and structure fields are integers too (`{267: [...]}` rather than
`{"leds": [...]}`). **These numbers are not stable across agent releases**,
confirmed: an S88/S61 MK3 on agent 2.0.7 (R15), IPC protocol 2.1.0, used
`connect_device`/`client_request_focus`/`client_lightguide_set_leds` = `382`/`373`/`360`;
on agent 2.1.5 (R14), protocol 2.2.0, those same three numbers all came back
`"Method not registered"` (first spotted by @Bounga, issue #18 comment 5208665711, then
reproduced here). Renumbering happens silently, nothing before this pointed at it.

What *is* stable is the name. The hello reply carries the service's entire
`symbol_registry`: an array of every method and field name it knows, several hundred
entries long. **A method's number is simply its index in that array.** Confirmed by
resolving `connect_device`'s index this way against the 2.1.5 service and calling it:
accepted, `[1, msgid, None, True]`, no hardcoded number involved. `ODRClient.m` and
`odr_lightguide.py` both resolve method numbers from this registry on every connection
now, not from baked-in numbers, see "Capturing more of it" below for how the current
names were found, in case they drift again.

| Name | Meaning | Kind |
|---|---|---|
| `"instance_hello"` | handshake, the only string-named method | request |
| `connect_device` | connect/attach to a device | request, replies `True` |
| `client_request_focus` | request focus | notification |
| `client_lightguide_set_leds` | set key LEDs | notification |
| `client_midi_addressing` | the LED array field inside `client_lightguide_set_leds`'s params | field |

Calling a notification-only method as a request instead returns
`Method not registered: {}`, handlers are registered per message kind. A malformed
argument shape for a real method returns `Arguments cannot be parsed: [...]` instead,
this is how the `client_midi_addressing` field name was found: a plausible-looking
guess (a bare positional array) got that error; real Komplete Kontrol traffic showed
the actual shape.

## Bringing up a session

```
c->s  <16 raw UUID bytes>
c->s  [0, 0, "instance_hello", [uuid, {client_info: {name, version, type: "standalone"},
                                       ipc_protocol_version: "2.1.0"}]]
s->c  [1, 0, None, {agent_version, ipc_protocol_version, available_devices: [...],
                    symbol_registry: [...], ...}]
      # resolve iConnect = symbol_registry.index("connect_device"), etc. from here
c->s  [0, 1, iConnect, [uuid, serial]]   -> [1, 1, None, True]
c->s  [2, iFocus, [uuid, serial]]        # focus
c->s  [2, iLeds, [uuid, serial, {iAddr: [[note, colour], ...]}]]
```

The hello reply enumerates devices with product name, serial, vendor and product ID, take
the serial from there; every later call is addressed by it.

Attaching answers `[1, msgid, nil, true]`, and a serial the service does not recognise gets
`[1, msgid, nil, false]` rather than an error, worth checking, since carrying on from a
refusal means every later LED message is accepted and ignored.

**Focus is required.** Without the `client_request_focus` notification the service accepts
`client_lightguide_set_leds` silently and nothing on the keyboard changes. This was the
second dead end: LED messages structurally identical to Komplete Kontrol's, ignored, while
stale colours from the last real KK session stayed latched on the device and looked like our
own output.

## The LED array

`{client_midi_addressing: [[index, colour], ...]}` with **128 entries, indexed by MIDI note
number**, not by physical key. An S88 occupies MIDI 21 (A0) through 108 (C8); smaller
keyboards take a sub-range. Sending 88 entries as `0..87` lights A0 up to D#6 and leaves the
top 21 keys dark, which is a convincing-looking wrong answer.

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

Each `client_lightguide_set_leds` call replaces the entire array; there is no per-key delta.

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

The cost is a hard dependency on `NIHardwareConnectionService` running. Method numbers
shifting between agent releases (confirmed to actually happen, see "Method numbering"
above) no longer needs a code change to survive, since both clients resolve numbers from
the hello reply's `symbol_registry` by name instead of hardcoding them. The one thing that
would still break lighting is the service dropping one of the four names this app depends
on outright, which would surface as a loud, specific error (`ODRClient.m`'s
`connectToDeviceWithSerial:` checks for exactly this) rather than a silent no-op.

## Capturing more of it

The service does report errors for malformed requests
(`Arguments cannot be parsed: [...]`, `Method not registered: {}`), so replies are a usable
oracle, but only for requests, not notifications, which covers most of the interesting
surface. To learn a new message, watch Komplete Kontrol perform the action through a relay
that moves the socket aside, binds the original path, and forwards to the real one:
`scripts/odr_relay_capture.py` does exactly this and logs every decoded frame in both
directions. This is how `client_midi_addressing` and the current method names were found
after the 2.1.5 renumbering: the relay caught real Komplete Kontrol traffic loading an
instrument, which is what actually resolved the ambiguity guessing couldn't.
Remember the 16-byte preamble when parsing the client side.

Beyond lighting, the same session carries the screens, mixer track data, browser model,
plugin chain and parameter pages, the full PLUG-IN mode feature set that raw USB bulk
never yielded, already decoded and served by the agent.

## What actually drives the MK3 screen

The MK3 screen is model-driven rather than exposing a framebuffer method. Relaying a real
Komplete Kontrol session while switching between plug-in, browser, and MIDI-settings views
found no `screen`, `pixel`, `bitmap`, or framebuffer-style operation in the 437-name
`symbol_registry`. That initially made arbitrary imagery look impossible, but the asset
path inside the parameter-page model turns out to be much more capable than its API names
suggest.

There are roughly 30 `client_*_set_*` methods that push structured DAW-model state, and
the device (or the agent, this session can't tell which renders) draws its screen from
that:

- `client_parameter_page_set_parameter_value`: `{value: 0.0, display_value: "C"}`, a
  numeric value plus its human-readable label, sent per parameter as it changes.
- `client_mixer_set_track_data`, `client_browser_set_data_model`,
  `client_plugin_chain_set_data`: full structured snapshots of mixer, browser, and plugin
  state, track names, presets, filter categories, all as plain strings and numbers.
- `nks1_layout`/`nks2_layout`: named layout templates used to populate device-owned
  screen layouts.

Images are content-addressed assets. `add_asset` accepts a SHA-256 digest and the encoded
asset bytes; `registered_assets` in the hello reply lists cached digests. Structured
payloads reference an asset by its digest rather than embedding its bytes.

### Verified arbitrary-image path

On 2026-08-12, `scripts/odr_display_probe.py` used a schema captured from Komplete
Kontrol 2.2.0 traffic to test that path against an S88 MK3 and Hardware Connection
Service 2.1.5:

1. Connect to the keyboard and request focus using the same lifecycle as the working
   light-guide client.
2. Upload two deterministic lossless WebPs with `add_asset`. Both are `1280x212`; their
   payloads are 896 and 926 bytes because the test patterns compress extremely well.
3. Select the `parameter` page with `client_set_page`.
4. Put the asset digest in `plugin_data.background` and send it through
   `client_parameter_page_set_data`.

The first magenta/cyan checkerboard appeared upright across the complete 1280x212 banner
region. A photograph confirmed the correct colors, orientation, and width; the lower part
of the 1280x480 screen remained black. This proves ODR can display arbitrary user-supplied
imagery, despite having no framebuffer-named method.

The probe then alternated two already-uploaded assets for 12 seconds at each requested
rate. Operator-visible results were:

| Requested rate | Sends | Client send time | Visible result |
| --- | ---: | ---: | --- |
| 0.5 FPS | 6 | 12.002 s | Clean alternation |
| 1 FPS | 12 | 12.005 s | Sent, but not operator-scored |
| 2 FPS | 24 | 12.002 s | Clean; a very brief dark interval was possible but uncertain |
| 5 FPS | 60 | 12.002 s | Clean alternation |
| 10 FPS | 120 | 12.005 s | Clean alternation; described as working great |

These methods are notifications, so the timings measure client dispatch rather than a
device acknowledgement. The visual observations are what establish that frames actually
changed. The service reported 100 registered assets before the ladder and again after the
probe disconnected and the inventory client reconnected, confirming that resending the
same two hashes was deduplicated.

`scripts/fixtures/odr_display_schema_2_2_0.json` preserves the captured field shape
without serial numbers or raw session data. `scripts/test_odr_display_tools.py` covers
framing, registry resolution, binary extraction/redaction, stale-relay recovery, stable
test images, and schema substitution.

**Conclusion**: arbitrary partial-height imagery is verified at up to 10 requested FPS.
This is not yet a full-screen framebuffer or an integrated Synthesia mirror: the proven
region is 1280x212, focus/page ownership still needs lifecycle work, and actual Synthesia
capture/encoding has not been connected to this path. It is nevertheless a viable route
to a useful MK3 display feature and overturns the earlier conclusion that ODR was limited
to icons, thumbnails, and text.
