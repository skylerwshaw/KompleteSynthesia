# 1. MK3 Screen via ODR: replay a captured page frame on the shared focused client

Date: 2026-08-20
Status: Accepted

## Context

The MK3 screen is an asset-backed rendering service, not a framebuffer (see
`MK3_VIDEO_RESEARCH.md`, `MK3_COMPATIBILITY.md`). Putting an image on it means storing the
image as an asset (`add_asset`) and sending a parameter-page model whose `background` field
references it (`client_parameter_page_set_data`). Two facts constrain how:

1. **The page-data frame is large and the service's parser is fragile.** Its arguments go
   through strong-typed msgpack converters, and a shape it dislikes throws an uncaught C++
   exception that aborts the *entire* `NIHardwareConnectionService` (the `sha256` handle
   type is the known trigger). That service also drives the light guide, so a bad screen
   write takes lighting down with it until the service relaunches. Synthesizing the frame
   from scratch risks tripping this; replaying a real captured frame byte-for-byte does not.
   A third party (Bounga) proved the replay approach end to end on an S61 MK3.

2. **Focus is a single per-client token** required by both lighting and screen writes, held
   by one client at a time. The app already holds one focused `ODRClient` session for
   lighting. A separate screen client would fight that focus and silently kill one channel.

## Decision

- **Display by replaying a captured `client_parameter_page_set_data` frame**, substituting
  only two equal-length spans (the 36-byte instance UUID and the 32-byte asset handle).
  Do **not** synthesize the frame for v1.
- **Add the screen methods onto the app's existing single focused `ODRClient`.** Do not open
  a second connection and do not run a refocus loop; the app holds focus for the session.
- **Always send the asset handle as exactly 32 `bin` bytes**, guarded in code, because a
  malformed handle crashes the shared service (and thus the light guide). This guard is
  permanent: we assume NI will not fix the underlying crash.

## Consequences

- Proven-correct and avoids the service crash; lighting and screen share one focus with no
  contention.
- The display path depends on a **bundled captured frame**, which is brittle across agent
  releases and must be produced with a one-time capture on real MK3 hardware.
- Unless the template is captured from a page with an empty parameter block, the captured
  preset's macro labels render beneath the image (a cosmetic wart).
- **Frame synthesis is tracked** as the path that removes the bundled capture and the
  labels; revisiting this decision is expected once synthesis can survive the parser.
