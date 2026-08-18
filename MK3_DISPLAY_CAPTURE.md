# Capturing the MK3 display protocol

The procedure for a capture session, what is already settled without hardware, and what a
capture has to answer. Background: `MK3_COMPATIBILITY.md` for the mission, `ODR_PROTOCOL.md`
for the socket.

## The goal, stated precisely

The MK3 half that still does not work is notes scrolling on the keyboard's screen. Lighting
the right keys is done and verified.

Those two are not independent, and it is worth being explicit about why, because it decides
what a solution can even look like. Synthesia's lighting stream carries note-on and note-off
for the key to press *now*. It carries no lookahead. There is nowhere to read the notes that
are coming, so there is nothing to draw from.

How does MK2 scroll notes, then? It does not draw them. `VideoController.m` calls
`CGWindowListCreateImage` on Synthesia's own window and copies its pixels. That is what the
app's "Screen Recording" permission is for. The scrolling exists only as pixels Synthesia
has already rendered.

**So the scrolling-notes goal requires pushing arbitrary pixels at the screen.** If that is
not possible, no amount of protocol work substitutes for it.

## The MK2 pipeline, for reference

What a working implementation looks like on the generation where it works:

1. `SynthesiaController synthesiaWindowNumber` finds the window.
2. A loop on a dedicated queue calls `CGWindowListCreateImage` on it.
3. `renderOverlayOntoCGImage` composites the volume OSD when visible.
4. `NIImageFromCGImage` drops the first `kHeaderHeight` (26) rows to lose the title bar,
   scales with `vImageScale_ARGB8888` if the window is larger than the screen, and converts
   with `vImageConvert_AnyToAny` to `bitsPerComponent 5, bitsPerPixel 16,
   kCGBitmapByteOrder16Big`, that is RGB565 big-endian.
5. `encodeImage` frames it: command byte `0x84`, screen index, an `x/y/width/height`
   rectangle in `htons`, the payload length counted in 32-bit longs, the pixels, a trailer.
   Note it supports partial rectangles.
6. `bulkWriteData` over USB, then `waitForBulkTransfer`.

Steps 1 to 4 are transport-independent and would be reusable. Steps 5 and 6 are what an
ODR route would replace.

**The numbers that matter.** An MK2 screen is 480x272 RGB565, so 261,120 bytes per frame. A
1280x480 MK3 screen, if `VideoController.m`'s untested guess is right, would be 1,228,800
bytes, roughly 4.7x per frame, at whatever rate makes scrolling look like scrolling.

## What the registry already settles, without hardware

Asking agent 2.1.5 (R14) for its symbol registry, 437 entries, with no keyboard attached:

**There is no framebuffer method of any kind.** `video`, `mirror`, `frame`, `buffer` and
`blit` match nothing. On MK3 a client does not own the panel's pixels.

What is there is a rendering service. The device owns the renderer (`restart_renderer`) and
clients push structured models:

| Symbol | What it suggests |
|---|---|
| `client_set_page`, `client_page_set` | choosing which page the device shows |
| `client_parameter_page_set_data`, `client_parameter_page_set_parameter_value` | the parameter view's contents |
| `client_browser_set_sounds`, `client_browser_set_sound_item` | the browser view's contents |
| `client_mixer_set_track_data`, `client_mixer_set_meters` | the mixer view's contents |
| `client_notification`, `send_global_notification` | transient messages |
| `assets_add`, `assets_remove`, `assets_reset`, `add_asset` | a client-supplied asset store |

This matters because `TODO.md`'s display item reads "not started at all", and
`VideoController.m`'s MK3 path is modelled on MK2: build a framebuffer, push it over bulk.
That model has no counterpart on this transport. The item is less "not started" than "the
planned approach does not match what the service offers".

**Where the image fields actually are.** The registry is grouped by structure, and the
grouping is informative:

```
 68  thumbnail_image        117  nks1_layout
 73  icon                   118  nks2_layout
 74  asset_image            119  background
 75  is_user_saved          120  parameters
 76  is_favorite            121  plugin_color
                            122  nks_control_color
                            123  r
                            124  g
                            125  b
                            126  is_rgb565
```

The three image fields sit between `is_user_saved` and `is_favorite`, which is the shape of
a preset list item rather than a screen. And `is_rgb565` is adjacent to `r`, `g`, `b`, so it
most likely flags how a *colour* is packed, not an image format.

Treat that last reading as an inference from adjacency, not a fact. It is a grouping
heuristic, and the capture is what settles it. But it does mean the registry alone gives no
evidence that a full-screen bitmap can be pushed, and `background` in the layout model is
the field most worth watching: whether it carries a colour struct or a blob decides a lot.

Reproduce any of this with `./scripts/odr_hello.py --grep '...'`. Read-only, no keyboard,
safe with Komplete Kontrol running.

## The three questions a capture has to answer

1. **Does any asset accept a full-screen image, and what is the size ceiling?**
2. **Is there a page that shows an arbitrary image full-bleed?** Or is the screen only
   reachable through native pages fed structured data?
3. **At what cadence does Komplete Kontrol actually push assets?** Even if 1 and 2 pass,
   an `add`/`remove`/`reset` store looks designed for a preset cover that lives for
   minutes. If a full re-push is the only update path, the result is a slideshow, not
   scrolling. This is the question that was missed at first and it is not secondary.

Guessing does not resolve any of these. The service answers `Arguments cannot be parsed`
without saying what it wanted, which is what forced the relay approach for the lighting
protocol too (`ODR_PROTOCOL.md`, "Capturing more of it").

## The tools

| Script | Job |
|---|---|
| `odr_hello.py` | One handshake, read-only. Devices with product IDs, versions, registry filtering. |
| `odr_capture.py` | The relay. Resolves method *and field* numbers to names, keeps raw wire bytes per frame, spills payloads to files so an asset push does not drown the log. |
| `odr_analyze.py` | Tallies frames by method, decodes blobs as RGB565 at every plausible size, writes a PNG per candidate. |
| `odr_report.py` | Renders a capture as a protocol write-up: method inventory with registry indices, annotated hexdumps, payload sizes, observed rates. |

All four have offline self-checks, `--selftest` on each, that need no hardware.

## Before starting

1. **Quit Komplete Kontrol completely.** An already-open connection bypasses the relay, so
   every client has to reconnect through it. The dock and window state are not a reliable
   check; `ps aux | grep -i "Komplete Kontrol"` is.
2. Leave `NIHardwareConnectionService` running. It is what we relay to.
3. Attach the keyboard directly, no hub.
4. `pip install msgpack` if needed.

Then confirm the service sees it, and settle the outstanding re-run request from the 2.1.5
method-ID fix at the same time:

```
./scripts/odr_hello.py          # device line names the product ID
./scripts/odr_lightguide.py     # 20 seconds, lights keys, changes nothing
```

## Taking the captures

Each capture is one action, in its own directory, so the diff between them is legible.
Komplete Kontrol must be launched **after** the relay is up, every time.

```
./scripts/odr_capture.py --out captures/NN-name
# launch Komplete Kontrol, perform the action, quit it, then Ctrl-C the relay
```

| # | Directory | Action |
|---|---|---|
| 1 | `captures/01-boot` | Launch, let it settle until the device shows its normal display, quit. Captures the whole bring-up, including whatever draws the default screen. |
| 2 | `captures/02-idle` | Launch, touch nothing for 60 seconds, quit. Establishes the noise floor, which is what makes question 3 answerable: every other capture's rate is read against this. |
| 3 | `captures/03-browse` | Launch, open the browser, load an instrument that has artwork, quit. The most likely place a real image asset crosses the socket. |
| 4 | `captures/04-pages` | Launch with that instrument loaded, press BROWSER / PLUG-IN / SETUP, then turn an encoder **continuously** for 20 seconds without pausing. The continuous motion is deliberate: it shows whether asset traffic tracks a moving display or stays flat. |

Nothing is written to the device at any point. The relay forwards bytes unmodified in both
directions and only observes them.

## Then

```
./scripts/odr_analyze.py captures/03-browse
./scripts/odr_report.py  captures/03-browse > captures/03-browse/report.md
```

Raw pixels carry no dimensions, so `odr_analyze.py` recovers candidate sizes by factoring
the byte count and writes a PNG for each. The right one is obvious on sight and the wrong
ones are visibly sheared, which is a faster oracle than reasoning about it.

## Active probing

Passive capture shows what Komplete Kontrol does. It does not show what the service would
*accept*, which is the larger question. The service is a usable oracle for that: it answers
`Method not registered` when a method does not exist in that message kind, and `Arguments
cannot be parsed` when it exists but the argument shape is wrong. That is how
`client_midi_addressing` was found.

So, with focus held: push assets of increasing size to find the ceiling, walk page indices
and observe what the screen shows, probe which methods accept which shapes.

**Off limits.** The registry carries device-management methods that have no place in this:
`reboot_for_dfu` (firmware update mode, the only thing here that could actually damage a
keyboard), `reboot`, `enable_serial_console`, and settings writes via `set_device_settings`
and `settings_set`. Stay on the client-facing surface. The worst realistic outcome of a
malformed client-facing probe is a service to restart or a keyboard to replug; nothing
written persists.

## If the relay is interrupted uncleanly

It moves the agent's socket aside and restores it on exit, including on Ctrl-C and SIGTERM.
Only a hard kill can strand it. To restore by hand:

```
cd "/Users/Shared/Native Instruments"
mv com.native-instruments.odr_agent.kks.real com.native-instruments.odr_agent.kks
```

If the relay reports it could not bind, it leaves the agent's own socket untouched and says
where the moved-aside original is; nothing is lost, and the same `mv` applies once the stale
socket is removed.
