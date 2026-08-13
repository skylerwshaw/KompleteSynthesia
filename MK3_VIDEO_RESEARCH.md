# MK3 flowing-video research

Last updated: 2026-08-12

## Goal and current boundary

The goal is fluid, natural Synthesia motion on a Kontrol S-Series MK3 display, ideally
30 FPS or better. It is not merely the ability to display arbitrary static artwork.

The verified ODR path uploads encoded images as content-addressed assets and selects them
as a 1280x212 parameter-page background. Static WebP works. Cached asset changes are
reliable through roughly 15 requested FPS, but motion is visibly stuttery; 20 FPS drops
or unevenly presents frames, and 30 FPS flashes badly. Animated WebP displays only its
first frame through both ODR and MIDI-template `image_data`. The 437-name ODR registry
contains image/asset model fields but no framebuffer, pixel, rectangle, surface, blit, or
video-stream operation.

Therefore ODR asset swapping remains a useful fallback, not a path to natural video. It
does not establish a hardware display limit: avenue 3 below now has a confirmed smooth
30 Hz native-renderer animation, so the ceiling is asset-swap-specific, not universal.
The following four avenues track both the tested result and the remaining work.

## 1. Test the legacy raw RGB565 rectangle protocol on MK3

### Why it may work

MK2 screen mirroring is not compressed-video playback and does not create image files.
The host captures a frame, converts it to RGB565, and sends a command containing the
screen number and an `x`, `y`, `width`, `height` rectangle followed by raw pixels and a
blit command. qKontrol documents and implements this as command `0x84` over USB bulk
endpoint 3:

- <https://github.com/GoaSkin/qKontrol/blob/master/source/qkontrol.cpp#L1042-L1096>
- <https://github.com/terminar/rebellion>

KompleteSynthesia already has the MK2 encoder and a 40 FPS capture loop in
`KompleteSynthesia/VideoController.m`. It contains speculative MK3 dimensions of
1280x480 and `KompleteSynthesia/USBController.m` has the confirmed MK3 interface 3,
endpoint `0x04`. `AppDelegate.m` currently prevents MK3 from instantiating this code, so
the app's continuous legacy display path remains disabled; the guarded one-shot result
is recorded below.

A 1280x212 RGB565 band is 542,720 bytes per frame, or about 16.3 MB/s at 30 FPS. That is
plausible over USB 2.0. Full 1280x480 RGB565 is 1,228,800 bytes per frame, or about
36.9 MB/s at 30 FPS, so partial rectangles and dirty-region updates would be important.

### Decisive experiment

Build a separate guarded probe; do not enable the existing full mirror path. With the
operator explicitly ready:

1. Stop Hardware Connection Service so interface 3 can be claimed exclusively.
2. Send one known 64x64 RGB565 rectangle using the legacy `0x84` command on endpoint 4.
3. Send nothing else and inspect the display.
4. Release the interface and restore Hardware Connection Service.

The guarded implementation for this experiment is
`scripts/mk3_raw_display_probe.c`, built by
`scripts/build_mk3_raw_display_probe.sh`. Its default and `--selftest` modes do not
enumerate or open USB devices; `--inspect-device` performs detection only and does not
open an interface. Its armed mode accepts only an exact confirmation token and can issue
only one fixed 64x64 checkerboard write: there are no geometry, payload, loop, retry,
clear, or reset options. The live mode must not be invoked until the operator has
explicitly confirmed they are watching the keyboard.

`scripts/run_mk3_raw_display_probe.sh` is the equally guarded live wrapper. It refuses
to run unless Hardware Connection Service is initially running, arms service restoration
before terminating that exact process, waits at most five seconds for shutdown, invokes
the fixed probe once, and restarts the service on success, failure, or interruption. It
does not escalate to force-killing a stuck process.

### Result — closed as a direct compatibility path (2026-08-12)

The exact one-shot experiment was performed twice on the connected S88 MK3 (USB product
`0x2120`). Both synchronous transfers completed successfully: all 8,228 bytes reached
interface 3 bulk OUT endpoint 4, whose reported maximum packet size was 512 bytes. The
interface was released and Hardware Connection Service restarted and reclaimed it after
each attempt without a keyboard reconnect.

The operator observed no checkerboard, flicker, blanking, or other display change on
either attempt; the keyboard remained on “Default MIDI template.” This rules out the
bare MK2 `0x84` rectangle packet as a directly backward-compatible MK3 framebuffer
command. It does **not** rule out raw pixel transport inside MK3's length-prefixed
`DISPLAY`/MessagePack protocol. That possibility moves to avenue 2.

The original success criterion was a colored rectangle appearing without replacing the
surrounding display. That criterion was not met, so no cadence or full-frame legacy test
is warranted. The probe did not alter stored settings or firmware.

## 2. Decode MK3's raw `DISPLAY` messages

**Status: closed as a direct natural-video path.**

### Evidence and uncertainty

Independent MK3 reverse engineering confirms interface 3 endpoint 4. It originally
described the raw transport as a length prefix, four-byte message category, and
MessagePack payload, but the decoder and live capture below refine that interpretation:
the supposed category is the beginning of one complete MessagePack-RPC message. The
public work identifies `93025492` as `DISPLAY`, but has not decoded a graphics primitive:

- <https://github.com/HugginsIndustries/kontrol-s88-mk3-linux/blob/main/docs/REVERSE_ENGINEERING.md>

The available startup capture's `DISPLAY` packet is small structured state rather than a
pixel buffer. Much of this raw protocol appears to be the device-facing counterpart of
the model-driven ODR methods already captured. This avenue may therefore converge on the
same asset-selection ceiling, but it has not been proven.

### Offline decoder and initial result (2026-08-12)

`scripts/mk3_usb_capture_decode.py` now parses Linux usbmon PCAPNG directly, extracts
data-bearing bulk transfers, splits multiple NI length-prefixed frames within a single
USB write, and decodes each complete MessagePack-RPC object. Bytes previously identified
as four-byte categories are actually encoded RPC prefixes: `93025492...`, for example,
begins `[2, 84, [...]]`, a notification to numeric method 84 (`DISPLAY`). Binary values
are hashed and length-bounded by default. Its synthetic capture tests are in
`scripts/test_mk3_usb_capture_decode.py`.

Against the genuine public `openCloseKompleteKontrol.pcapng` startup capture, it decoded
21 host-to-device frames. Only two used RPC method 84 (`DISPLAY`), and neither carried a
pixel buffer:

- a 16-byte wire frame containing `14, {180: [], 181: 0}`;
- a 66-byte wire frame containing one structured item named `Empty`, a 16-byte identifier,
  six boolean/null fields, and selected index zero.

This is consistent with a device-side plugin-chain/display model, but startup traffic is
not enough to close the avenue because it never changed an image.

macOS exposes no passive USB capture interface on this host. The prepared correlation
path therefore attaches LLDB to Hardware Connection Service's symbolized
`usb_odr_interface::bulk_write_helper::write_to_bulkpipe`. Offline disassembly verifies
the arm64 ABI at that boundary as endpoint in `x2`, span address in `x3`, and span length
in `x4`. `scripts/lldb_mk3_usb_capture.py` copies those outgoing bytes into a standard
PCAPNG and immediately resumes the service; `scripts/attach_mk3_usb_capture.sh` applies
the breakpoint to the running service. The callback caps any read at 16 MiB and is
fail-open so a capture error does not stop the service.

The isolated live action is `odr_display_probe.py trace-static`: it verifies its 896-byte
test WebP is already registered, then sends only connect/focus, parameter-page selection,
and one model referencing the cached digest. It performs no `add_asset` and no second
display update. Plan-only validation succeeded without focus or display changes. The
debugger attach and visible action still require explicit operator confirmation.

The first confirmed live attempt did not reach the capture or display-action phases:
macOS denied LLDB permission to attach to the already-running NI-signed service because
it has hardened runtime enabled. No breakpoint was installed, no PCAPNG was created, and
the cached checkerboard was not selected; the original service continued owning the USB
interface normally.

The guarded fallback uses
`scripts/prepare_mk3_usb_capture_service.sh` to place a copy under the ignored `build/`
tree and ad-hoc sign that copy without hardened runtime. The installed NI bundle remains
untouched. `scripts/run_mk3_usb_correlation_capture.sh` requires an exact operator-ready
token, verifies a new `/private/tmp/*.pcapng` target and the copied signature, arms
restoration, stops the original service, launches only the temporary copy under LLDB,
runs the one cached-image selection, terminates the copy, and restores the original
signed service. Static validation resolves exactly one breakpoint location at
`bulk_write_helper::write_to_bulkpipe`; the prepared copy's signature verifies as
`adhoc` and has no hardened-runtime flag.

The first confirmed fallback run armed the hook and captured the temporary service's
8,098-byte startup notification `[2, "handshake", params]`. It did not select the cached
checkerboard: the probe connected before device registration finished, saw zero devices,
and refused before focus or display writes. The temporary service logged the keyboard
hotplug a few seconds later, proving this was a startup race rather than a USB ownership
failure. The original signed service was restored automatically. The wrapper now waits
read-only for a fresh service hello to report exactly one device before permitting the
guarded static selection.

The second confirmed fallback run also refused before focus or display writes, but for a
different reason: both the temporary copy and the subsequently restored signed service
stalled before USB initialization. A process sample located the block in
`extract_cubase_script_version` -> `fopen` -> `open$NOCANCEL` while HCS performed its
unrelated Cubase MIDI Remote script check. macOS TCC logs show that the ad-hoc copy, which
still shared NI's bundle identifier but not its signing requirement, initiated a
`SystemPolicyDocumentsFolder` prompt that remained unresolved after the copy terminated.
The signed service's later Documents access is queued behind that orphaned request. Both
the bundled and installed JavaScript files are intact and readable by ordinary processes.
No PCAPNG was produced in this run. Future debug copies must use an isolated application
identity or bypass this unrelated startup check so they cannot disturb NI's existing
Documents permission record.

The operator later approved the pending Documents requests. The already-running signed
service immediately completed the Cubase version check, initialized its bulk pipes, and
completed the S88 handshake; a read-only ODR hello again reported serial `DF745481`.
Therefore no TCC reset was required. The existing ad-hoc copy was also among the approved
requests, but a future capture must still treat any renewed permission prompt or startup
delay as a refusal condition and restore the signed service without a display write.

### Successful cached-background correlation (2026-08-12)

The third confirmed fallback run completed end to end. The temporary service registered
serial `DF745481`; `trace-static` selected the cached 896-byte checkerboard once, held it
for three seconds, and the wrapper restored the signed service. The resulting 11 KiB
PCAPNG contains 18 cleanly decoded host-to-device frames. The 8,098-byte symbol-registry
handshake dominates the capture; all 17 post-handshake frames total only 1,014 bytes, and
the largest is 252 bytes.

The actual background selection is a 51-byte
`client_instance_parameter_page_model_set_plugin_data` notification. Its named fields are
`name = "KompleteSynthesia ODR Probe"`, `background = 28`, empty `parameters`, and null
plugin/control colors. Here `28` is an opaque device asset handle, not symbol-registry
entry 28. Adjacent traffic consists only of `client_page_set`, host/device viewstate
models, focus/instance setup, and instance removal. There are no WebP bytes, hashes,
pixels, rectangles, strides, damage regions, presentation timestamps, queue-depth fields,
or buffer-swap flags.

This proves that cached ODR selection maps directly to the MK3's structured device-side
renderer model rather than exposing a hidden framebuffer presentation primitive. It
substantially narrows avenue 2.

### Successful initial-asset correlation (2026-08-12)

The final bounded experiment uploaded one previously unregistered 1,132-byte WebP, then
selected it once. The resulting 12 KiB PCAPNG contains 19 cleanly decoded frames, and the
signed service again reported `DF745481` after automatic restoration.

The upload maps to exactly one 1,180-byte `assets_add` notification:

- opaque device asset handle `121`;
- the expected 32-byte SHA-256 digest
  `31595cac18db60eff1abb18d793a1181e838b26a421a5b4c7a0d5438b37dfda3`;
- the expected 1,132 WebP bytes, byte-for-byte identical to the ODR payload.

Exactly 7.744 ms later, the same 51-byte
`client_instance_parameter_page_model_set_plugin_data` shape sets `background = 121`.
No other asset or presentation message occurs. There are no pixels outside the encoded
WebP, rectangles, strides, damage regions, timestamps, queue-depth values, decode/present
acknowledgements, or buffer-swap flags.

The operator initially observed the newly uploaded image as vertical color stripes. A
separate repeat selected the identical cached asset once through the normal signed HCS,
without an upload, debugger, or service restart, and held it for six seconds. The full
striped image remained visible for the entire hold with no observed delay, dark flash, or
other artifact. This confirms that both first-time upload and later cached selection work;
the previously observed motion failures arise from rapid whole-asset replacement rather
than unreliable static image rendering.

### Conclusion

The raw MK3 transport is the device-facing form of the same asset/model architecture
already exposed by ODR. Direct USB access could remove host-service overhead, but it
would still upload a separately encoded static asset and then select its handle; it does
not reveal a video, framebuffer, partial-update, or scheduled-presentation primitive.
That cannot solve the observed 20–30 FPS flashing/stutter ceiling, so avenue 2 is closed
as a direct natural-video path. Proceed to avenue 3: drive lightweight native renderer
models and test whether their numeric/property updates animate fluidly on-device.

## 3. Drive native on-device renderer primitives

### Why it is different

NI describes MK3 as having an on-device rendering engine, whereas MK2 receives
host-rendered pixels. The current symbol registry includes `nks1_layout`, `nks2_layout`,
`display_type`, `waveform_parameter_style`, parameter values, and other structured UI
fields. Small numeric/model updates may be rendered or interpolated smoothly without
decoding and replacing a large background image.

This cannot provide arbitrary photographic video unless an undocumented canvas exists.
It may still provide the actual desired result: a fluid Synthesia-style piano roll drawn
from device-native bars, meters, waveforms, or animated parameter controls.

### Result — all eight knobs confirmed independent and smooth (2026-08-12)

`native-ramp --all-knobs` (new flag on `odr_display_probe.py`) drives all eight captured
knobs at once, each phase-staggered around the same 2-second triangle so simultaneous
independent motion is visible as a wave sweeping across the row rather than lockstep
movement. The confirmed live run dispatched all 1,440 messages (180 ticks x 8 knobs) in
6.004 seconds, zero late deadlines, no parser rejection. The operator watched all eight
animate independently and smoothly, no lag or stutter versus the single-knob run. This
rules out the single-knob result being a fast-pathed special case: the renderer handles
at least eight independently-addressed continuous parameters at 240 messages/second
combined without degrading.

### Result — 60 Hz confirmed smooth, all eight knobs (2026-08-12)

The same `--all-knobs` run repeated at `--fps 60` dispatched all 2,880 messages (360
ticks x 8 knobs, 480 messages/second combined) in 6.003 seconds, zero late deadlines, no
parser rejection. The operator watched and reported it smooth, no visible degradation
versus 30 Hz. Doubling both per-knob rate and simultaneous knob count did not surface a
send-loop or device-side ceiling; whatever the real limit is, it's above 480 msg/s.

### Next work

Remaining questions before this can represent Synthesia's falling notes:

- ~~Capture a real NKS2 instrument that visibly animates a waveform, envelope, meter, or
  other continuous graphic~~ Checked directly on hardware (2026-08-12): confirmed, real
  precedent exists (Hypha's Morpher X/Y), see below. It's ordinary knob animation, not a
  richer widget, so this doesn't reveal a new primitive beyond what's already proven, but
  it does confirm continuous background streaming to a page the user isn't looking at is
  normal, intended behavior, not a misuse of the protocol.
- Measure how much of the 1280x480 display a layout can address; the eight-knob capture
  used a single NKS1 row, not the full panel. This is the main open question now.

### Result — first check was on the wrong page; a real instrument does animate a device-native widget (2026-08-12)

The first direct check (turning Massive X's wavetable knob, ad hoc, no capture running)
showed nothing on the physical MK3 screen while Komplete Kontrol's own host-side plugin
GUI *did* animate. Read in isolation that looked like proof host-rendered graphics never
reach the hardware. A follow-up relay capture (`odr_relay_capture.py --output`) with
Hypha loaded caught the real explanation: a continuous stream of 1,179
`client_parameter_page_set_parameter_value` messages each for parameter ids 20 and 21, at
roughly 25-30 Hz, for the entire ~27-second capture, with no user input. The captured
`client_parameter_page_set_host_owned_viewstate` model resolves those ids: a `"Morpher"`
section with knobs `Animator, Shape, Speed, Direction, Retrigger, Sync, X, Y`, where
`Shape` (id 17) is a `discrete_parameter` listing `Square, Circle, Triangle, Line,
Lissajous1-4, Rose1-4`, and `X`/`Y` are ids 21/20, an animated X-Y morpher position Hypha
computes continuously regardless of what's currently shown on any screen.

That Morpher section is the third of three knob banks (Performance -> User -> Morpher),
not the page an instrument opens on. The operator was on the default Performance page
during the first check and the capture, so the stream was real the whole time, just to a
page not on screen. Navigating the physical hardware to the Morpher page confirmed it:
**the X/Y knobs visibly animate on the real MK3 screen**, driven by exactly the message
type this project's own probe already uses (`client_parameter_page_set_parameter_value`),
at a comparable rate to the confirmed-smooth 30 Hz all-knobs test above.

This reverses the conclusion from the first check. At least one real, shipping NKS2
instrument does drive continuous device-native knob animation on MK3 hardware, using the
same mechanism already proven here, real-world precedent that this is a supported,
intended use of the protocol rather than something being pushed past its design. It does
not prove a richer widget type exists (Hypha's animation is still two ordinary knobs, X
and Y, not a drawn curve or scope trace), so the open question from "Next work" about how
much of the 1280x480 panel a layout can address stands as before.
- Measure how much of the 1280x480 display a layout can address; the eight-knob capture
  used a single NKS1 row, not the full panel.
- If fixed layouts cannot place enough independent objects to represent falling notes at
  useful density, close this avenue for full Synthesia rendering, but a reduced
  representation (e.g. one bar/knob per near-term note) may still be worth scoping as its
  own smaller feature.

The first guarded probe is prepared in `odr_display_probe.py native-ramp`. It reproduces
the captured NKS1 parameter model with eight `continuous_parameter` knobs, no background
asset, and a standard `knob_parameter_style`. Only the first control, labeled `MOTION`,
changes: a tiny `client_parameter_page_set_parameter_value` notification sweeps its value
0 -> 100 -> 0 on a two-second triangle cycle at 30 updates per second for six seconds.
The other seven controls remain static. Plan-only resolution succeeded against the live
437-symbol registry without focus, page, model, or display writes. A live run remains
operator-confirmed.

The first confirmed run dispatched all 180 planned updates in 6.002 seconds with zero
late deadlines, but no knobs appeared: the keyboard changed from its default MIDI page
to `No instrument loaded`. Offline comparison with the real-instrument capture found two
specific model omissions. NKS1 layout slots are keyed by `id`, not `parameter_index`, and
the renderer requires a non-empty `client_plugin_chain_set_data` model to leave its
no-instrument state. The corrected probe now uses `id = 0..7` and follows the parameter
model with one synthetic `KompleteSynthesia Native Motion` chain entry plus the captured
trailing `Empty` entry. This corrected model requires a fresh confirmed live run.

That second confirmed run also produced no visible knobs. HCS diagnostics proved this
was another host-model error rather than a renderer result: it rejected the initial
model and every one of the 180 updates with `Arguments cannot be parsed`. A structural
diff exposed a mixed encoding rule in the accepted capture. Outer model fields use
numeric symbol-registry keys, while parameter variants and their children use literal
string tags (`continuous_parameter`, `parameter_style`, `knob_parameter_style`,
`parameter_value`, `value`, and `display_value`). The probe had incorrectly resolved
those nested tags to numeric IDs. The corrected version preserves the string-tagged
unions and inspects the HCS log after the initial model and a single zero-value update;
any parser rejection now aborts before the 30 Hz sequence.

### Result — confirmed working (2026-08-12)

The third confirmed run, with the string-tag fix, dispatched all 180 planned updates in
6.003 seconds with zero late deadlines and no `Arguments cannot be parsed` rejection.
This time the operator watched real hardware: the S88 MK3 left "No instrument loaded"
for a page titled "KompleteSynthesia Native Motion", and the bottom-left-most knob
(layout index 0, labeled `MOTION`) animated smoothly through its 0->100->0 triangle
ramp for the full six seconds. The operator described the motion as "super smooth,"
consistent with device-native rendering rather than the WebP-swap path's 20-30 FPS
flashing/stutter ceiling.

This closes the open question from avenue 3's "Next work" section: the on-device
renderer does animate a driven numeric value fluidly, at least for a single continuous
parameter at 30 Hz. It does not by itself prove enough independent objects exist to
represent falling notes, that remains open, see "Next work" below.

## 4. Analyze firmware and Hardware Connection Service renderer internals

### Targets

Perform static analysis before considering any firmware modification. Search firmware
and service binaries/resources for:

- video or animated-image decoders;
- framebuffer, shared-memory, texture, canvas, damage-region, or swap/present APIs;
- renderer RPC names omitted from the public registry;
- custom NKS2 layout definitions or loadable renderer modules;
- debug/development transports exposed over USB or a local socket.

The strongest outcome would be a supported but undocumented host-to-renderer primitive.
A firmware modification is a separate, much riskier project and must never be attempted
as an incidental experiment. No firmware writes, DFU/reboot commands, or persistent
system changes are authorized by this research note.

## Recommended order

1. ~~Prepare and run the single-rectangle raw USB probe from avenue 1.~~ Done, closed.
2. ~~Improve raw endpoint capture and decode the `DISPLAY` traffic from avenue 2.~~ Done,
   closed.
3. ~~Test native renderer animation from avenue 3.~~ Done, confirmed working: see avenue
   3's "Next work" for what's left before it can represent falling notes.
4. Continue static firmware/service analysis from avenue 4 only if avenue 3's remaining
   questions close it off; otherwise avenue 3 is the live path.

Before every visible hardware experiment, explain exactly what will run and wait for the
operator's explicit `ready`, `yes`, or `go`. Unexpected failures or materially different
tests require a fresh confirmation.
