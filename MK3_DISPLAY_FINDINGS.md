# MK3 display over ODR: what works, measured

Findings from a capture-and-probe session on a real S61 MK3 (agent 2.1.5 R14, IPC 2.2.0).
This is the "what we learned" companion to [MK3_DISPLAY_CAPTURE.md](MK3_DISPLAY_CAPTURE.md)
(how to capture) and [ODR_PROTOCOL.md](ODR_PROTOCOL.md) (the socket and lighting). Everything
below was reproduced on hardware, not inferred from the registry alone.

## Summary

A client can put an arbitrary image on the MK3 screen through the same ODR socket that
lights the keys, with no USB access and without disturbing the control surface. It is not a
framebuffer: the client stores an image as an *asset* and then sends a *page model* that
references it. Proven end to end, photographed on an S61 MK3.

| Question | Answer |
|---|---|
| Can a client draw an arbitrary bitmap on screen? | **Yes.** |
| How? | `add_asset` stores a PNG/WebP, `client_parameter_page_set_data.plugin_data.background` displays it. |
| What image format? | PNG and WebP both accepted; the service decodes them. Not raw RGB565. |
| What surface? | The parameter page's header band, **1280x212**, full width, roughly the top half of the panel. Not full screen. |
| Animation framerate? | ~3-4 fps re-encoding every frame; **~10-15 fps** when assets are preloaded and only the page frame is resent. |
| Cost? | A hard dependency on the page model's shape, and one NI-service crash bug (below). |

## The mechanism

The screen is a rendering service, not a pixel buffer. The registry has no `video`/`mirror`/
`frame`/`buffer`/`blit` symbol; it has a renderer the device owns (`restart_renderer`) and
client methods that push structured models. Images travel through a content-addressed asset
store and are referenced by handle from those models.

### Storing an image: `add_asset`

```
notify add_asset  [ <32-byte handle>, <image bytes> ]
```

- Registry index 390 on this agent. **It is a notification**, not a request; sent as a
  request it answers `Method not registered`.
- The handle is the **SHA-256 of the image bytes**, sent as a 32-byte msgpack `bin`. The
  store is content-addressed: the same image is the same handle, and handles persist across
  disconnects (the hello reply's `registered_assets` lists them, ~300 already present from
  Komplete Kontrol on this machine).
- The image is a normal encoded file. In one browse capture, 11 preset thumbnails were PNG
  (134x66) and one preset header was **WebP lossless, 1280x212, 260KB**. The service decodes
  both.

**Crash bug worth reporting to NI.** The handle is deserialised into a strong `sha256`
type (`std::array<std::byte, 32>`). A handle of the wrong shape (a UUID string, a 32-int
array, anything not a 32-byte bin) makes the msgpack converter throw a C++ exception that
is never caught: `__cxa_throw` -> `abort`, `SIGABRT` in `ServiceWorkerThread`, and **the
whole `NIHardwareConnectionService` process dies**, taking every client's session with it.
It relaunches in a few seconds and loses nothing, but any local process can crash it with
one malformed notification. Faulting frame:

```
msgpack::v3::adaptor::convert<strong::type<std::array<std::byte,32ul>,
  ni::odr::utils::sha256_tag, ...>>::operator()(msgpack::v2::object const&, ...)
```

Send the handle as exactly 32 `bin` bytes and it is fine.

### Displaying it: `client_parameter_page_set_data`

```
notify client_parameter_page_set_data
  [ <instance-uuid str>, <serial str>,
    { plugin_data: { name: <str>,
                     background: <32-byte handle>,   # the asset to show
                     parameters: { ... } } } ]
```

- Registry index 409. The `background` field (registry `background`, index 119) holds an
  asset handle. The device renders that image full-width as the page header, with the eight
  macro knobs and their labels drawn beneath it by the device itself.
- The frame is large and the argument parser is fragile (same strong-typed converters as
  above), so synthesising it from scratch risks the crash. What works reliably is to
  **replay a real captured frame byte-for-byte**, substituting only two equal-length spans:
  the instance UUID and the 32-byte background handle. `scripts/odr_display.py` does exactly
  this. Because the rest of the captured frame is left intact, the original preset's macro
  labels still show beneath the image.

### Bringing up a session

No separate instance handshake is needed; the instance UUID is just the client's own
connection UUID, the same one used for lighting.

```
<16-byte UUID preamble>
request instance_hello ...                          -> reply with registry + devices
request connect_device [uuid, serial]               -> true
notify  client_request_focus [uuid, serial]
notify  client_set_page [uuid, serial, <page idx>]  # 'parameter' page index
notify  add_asset [handle, image]
notify  client_parameter_page_set_data [uuid, serial, {plugin_data:{..., background: handle}}]
```

Focus is required, as for lighting: without `client_request_focus` the page frame is
accepted and nothing shows. The display lasts only while the client holds focus; on
disconnect the device returns to its normal (MIDI-template) screen.

## Framerate, measured

Software side is a non-issue: encoding a 1280x212 PNG is ~1.7ms, and `add_asset` +
page-frame send is ~0.9ms, so the pipeline sustains 400+ fps and the service never stalled
under it.

The device is the bottleneck. Two animations on hardware, filmed and analysed frame by
frame (video analysis is rough: the panel is glossy and the shots are off-axis, so treat
these as order-of-magnitude):

- **Re-encoding every frame** (`add_asset` + page frame per step): the device coalesces to
  roughly **3-4 updates/s**. Confirmed by eye ("jumps in threes" at 10 sent/s) and by
  counting distinct frames reached (11 of 40).
- **Preloading all assets first**, then looping only the page frame that swaps the
  referenced handle: visibly smoother, **~10-15 fps**, 20 of 30 distinct frames reached,
  median ~67ms on screen. Decoding was a large part of the cost.

Not yet smooth enough for fluid scrolling, but the preload result says the ceiling is
higher than the first test suggested. Untried levers: smaller images upscaled by the device,
and a lighter update path than resending the whole page model.

## What this means for the scrolling-notes goal

The original MK2 feature mirrors Synthesia's window as pixels. On MK3 that exact model has
no transport, but the goal is reachable a different way: render a Synthesia-style view
ourselves and push it as the page background. The open gaps are surface (a 1280x212 band,
not the full panel; whether another page exposes a larger image is untested) and smoothness
(the preload path needs pushing further).

## Tools

All under `scripts/`, all with offline `--selftest`:

- `odr_hello.py`: handshake, devices, registry filtering. Read-only.
- `odr_capture.py`: MITM relay that resolves method/field numbers to names, keeps raw wire
  bytes per frame, and spills payloads to files.
- `odr_analyze.py`: tallies methods, decodes blobs.
- `odr_report.py`: renders a capture as a protocol write-up with hexdumps.
- `odr_display.py`: puts an image on screen by the mechanism above (the reference POC).
