# Context glossary

Canonical vocabulary for KompleteSynthesia. Glossary only, no implementation detail.
When a term here conflicts with how code or a message uses a word, this file wins; fix the
code's language or update this file deliberately.

## Terms

**Light guide** — the physical per-key RGB LEDs above the keybed. Driven by the lighting
path (`setKeyColors:` / `client_lightguide_set_leds`). An independent channel from the
Screen: lighting and Screen coexist on one device session.

**Screen** (MK3) — the 1280x212 image band across the top of the MK3's parameter page,
which a host can set to an arbitrary image over the ODR socket. This is *not* the physical
key LEDs, and *not* a framebuffer. Distinct from Screen Mirroring. When we say "Screen" we
mean this MK3 feature.

**Screen Mirroring** (MK2) — the existing feature that captures the Synthesia application
window pixel-for-pixel and pushes it to the MK2 LCD screens over USB. Different device,
different transport, different mechanism from Screen. Do not call the MK3 feature
"mirroring."

**Page background** — the parameter page's full-width 1280x212 image slot that Screen
writes to. The device renders the eight macro-knob widgets beneath it.

**Asset** — a content-addressed encoded image (PNG or WebP) stored on the device. The
device decodes it; we never send raw pixels.

**Asset handle** — the 32-byte SHA-256 of an asset's bytes. It is both the asset's identity
and how a model (e.g. a page background) references it. Always exactly 32 bytes on the wire;
a wrong-shaped handle crashes the connection service.

**Focus** — the per-client ownership token the ODR service requires before it applies a
client's writes. Both lighting and Screen need it, and it is held by one client at a time.
The app keeps a single focused session for both.

**Now-playing notes** — the currently sounding notes derived from Synthesia's key-light
MIDI loopback: note name, octave, and hand (from the MIDI channel). This is the only live
per-song data the app receives. Synthesia does not send song title, playback progress,
tempo, or reliable play/pause over any channel available to us.
