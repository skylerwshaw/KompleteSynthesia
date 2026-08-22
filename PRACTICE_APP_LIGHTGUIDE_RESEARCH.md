# Alternative practice-app light-guide research

Last updated: 2026-08-22

Research for [issue #2](https://github.com/skylerwshaw/KompleteSynthesia/issues/2): whether any
mainstream piano-practice app exposes a MIDI (or other) light-guide protocol that communicates
true note-hold duration, unlike Synthesia's "Finger-based channel" feed, and whether such a
protocol would be legally usable by an open-source project like this one.

## Background

KompleteSynthesia's key-lighting and MK3 screen note-name band both render exactly what
Synthesia's MIDI light-guide feed sends (see [SETUP.md](SETUP.md#configuring-synthesia) for the
"Finger-based channel" configuration this depends on). A confirmed limitation of that feed: it
sends a note-off almost immediately after note-on regardless of the note's actual musical
duration, so a held whole note only ever produces a brief on/off pulse on the wire. This is a
property of Synthesia's own MIDI export, not a bug in how KompleteSynthesia consumes it — there
is no true-hold-duration information in the feed to render even in principle.

This document surveys other note-highlighting / piano-practice apps to see whether any of them
expose a richer, third-party-usable protocol that does carry true hold duration, and whether its
license terms would permit an open-source project to build against it the way this project
builds against Synthesia's feed.

## TL;DR

**No better integration target was found.** Of the four apps named in the issue (Flowkey, Simply
Piano, Piano Marvel, Yousician) plus two additional hardware-lighting-relevant apps checked
(Melodics, ROLI), none publishes an open, third-party-consumable MIDI light-guide *output*
protocol at all:

- **Simply Piano**, **Piano Marvel**, and **Yousician** are architecturally input-only: they
  listen to a connected keyboard (via camera/mic/MIDI) for note recognition and grading, and have
  no documented feature that sends light-guide data back out to a keyboard's LEDs or to
  third-party software.
- **Flowkey** does drive real per-key LEDs, but only through Yamaha's own closed "Stream Lights"
  hardware feature on specific Clavinova (CSP) models over a specific wired connection — not a
  documented, vendor-neutral MIDI light-guide message a third party could target.
- **Melodics** and **ROLI**, checked as the closest things to "richer lighting protocol" prior
  art because they do drive third-party MIDI hardware LEDs, turn out to light hardware only
  through closed, per-vendor integration presets (Melodics' own default-mapping list per
  supported device; ROLI's Dashboard app shipping a bespoke preset for its own Lightpad hardware)
  rather than a documented, open protocol a third party could target. Neither publishes a
  MIDI-message-level spec for its lighting behavior at all, so there is no evidence either one
  carries true hold duration — the protocol just isn't public, whatever it actually is (see the
  Melodics/ROLI section below for exact sourcing and the caveats on what remains unconfirmed,
  including an access limitation on ROLI's own site hit during this research).

Where terms of service were checked (Piano Marvel, Simply Piano, Yousician, Flowkey), every one
either explicitly prohibits reverse-engineering/unauthorized interoperation with no carve-out for
this kind of project, or is silent on the question because there is no output protocol to
interoperate with in the first place. None offers an affirmative developer/API program an
open-source hardware-lighting project could rely on.

**Bottom line for this project: Synthesia's pulse-only feed remains the best available
integration target.** The strike-pulse limitation is not something a competitor's app or
protocol currently solves; it would need to be addressed some other way (e.g. inferring hold
duration heuristically from Synthesia's own note data, or a feature request to Synthesia itself)
rather than by switching data sources.

## Sourcing note

Every claim below traces to a primary source: the vendor's own developer docs, published
Terms of Service / EULA, or their own marketing/support pages. Secondary write-ups were used
only to locate where a primary source lives, never as the basis for a claim. Where no primary
source could be found for a sub-question after a real search effort, that is stated explicitly
rather than filled with a guess.

## Synthesia (baseline)

Included for comparison, since this project already interoperates with it.

**Terms.** Synthesia LLC's own terms, published at its official site
[synthesiagame.com/legal](https://www.synthesia.io/legal) — checked 2026-08-22 — grant a
non-exclusive, non-transferable license and state Synthesia "retain[s] all proprietary rights to
the Application," but contain **no explicit reverse-engineering or third-party-interoperability
prohibition** in the visible terms text. This is a meaningfully more permissive baseline than
every competitor's terms checked below (all of which do explicitly bar reverse-engineering).

Note for anyone re-checking this: there is an unrelated same-named company, Synthesia (the AI
video/avatar generator) at `synthesia.io`, with its own, much stricter Terms of Service that
explicitly bar reverse-engineering. That is a different company and does not apply to the piano
app; take care not to conflate the two when searching.

## Flowkey

**Q1 — third-party light-guide output protocol?** No. Flowkey's only real per-key lighting
feature is Yamaha's own onboard **"Stream Lights"** hardware, available solely on specific Yamaha
Clavinova CSP models over a specific wired USB connection to the instrument. Flowkey's own
connection-help article states: "If your Yamaha instrument supports Stream Lights, they will
also work while connected via USB MIDI" —
[help.flowkey.com/en/articles/412902](https://help.flowkey.com/en/articles/412902-connect-your-instrument-to-your-android-device),
checked 2026-08-22. Flowkey's own Yamaha partnership page
([flowkey.com/en/yamaha](https://www.flowkey.com/en/yamaha), checked 2026-08-22) confirms this is
gated to that hardware and wired connection, and never names an open protocol, only the Yamaha
feature and cable. No flowkey primary source mentions any other brand (e.g. Casio) in connection
with key lighting; flowkey's own compatibility docs
([help.flowkey.com/en/collections/34751-compatibility](https://help.flowkey.com/en/collections/34751-compatibility),
checked 2026-08-22) never mention illuminated keys at all. Flowkey's public
`flowkey/midi-sound` GitHub package
([github.com/flowkey/midi-sound](https://github.com/flowkey/midi-sound), checked 2026-08-22) is a
MIDI-*in*-to-audio module (`noteOn`/`noteOff`/`setSustain`), confirming flowkey's own published
MIDI code is about receiving notes, not emitting a lighting scheme. This is a closed,
single-vendor hardware integration, not something a third party like KompleteSynthesia could
target.

**Q2 — true hold duration?** No public primary source found for the Stream Lights command
timing specifically; flowkey's own docs describe only the physical connection requirement, never
the message format sent to the instrument. (Tried: flowkey's MIDI-setup help collection, the
Yamaha/flowkey joint Clavinova page, and general searches for flowkey MIDI note timing
documentation — none describe the outbound message stream.) Moot in practice since Q1 already
rules this out as a usable third-party target.

**Q3 — terms.** Flowkey's Terms of Service
([flowkey.com/en/terms-of-service](https://www.flowkey.com/en/terms-of-service), checked
2026-08-22), Section 6, prohibit "Decompiling, reverse engineering or modifying the Service,"
"Circumventing technical protection measures," and "Copying, recording or making available any
content from the flowkey Service." Section 3.2 restricts use to personal, non-commercial
purposes without flowkey's express written consent for anything else. The ToS is silent on
MIDI-output interoperability specifically (because, per Q1, flowkey doesn't publish a light-guide
output to interoperate with) but its reverse-engineering clause would bar decompiling flowkey's
own app/service to learn how it talks to a Yamaha instrument.

**Conclusion:** Not a viable alternative target. No reusable protocol exists; only a closed,
single-vendor hardware feature.

## Simply Piano (JoyTunes)

**Q1 — third-party light-guide output protocol?** No. Simply Piano's MIDI support is
input-only: it listens to a connected keyboard for note recognition, it does not send light-guide
data out. JoyTunes' own help-center MIDI setup articles are all framed around "perfect note
recognition" (the app receiving notes), never an outbound signal to the instrument —
[piano-help.hellosimply.com/en/articles/2607992](https://piano-help.hellosimply.com/en/articles/2607992-step-2-usb-midi-cable-connecting-to-an-ios-device),
[.../2627738](https://piano-help.hellosimply.com/en/articles/2627738-step-1-using-a-midi-cable-with-simply-piano-for-ios),
[.../2627999](https://piano-help.hellosimply.com/en/articles/2627999-step-1-using-a-midi-cable-with-simply-piano-for-android),
[.../8147838](https://piano-help.hellosimply.com/en/articles/8147838-connect-to-midi-over-bluetooth)
(all checked 2026-08-22). Simply Piano's own App Store marketing copy touts "Real-time key
highlights: Play along as the keys light up, guiding you through each song," but this refers to
the **on-screen virtual keyboard inside the app**, not a hardware LED output — nowhere in the
listing or help center does JoyTunes claim to drive a connected keyboard's physical LEDs.
([apps.apple.com/us/app/simply-piano/id6503691129](https://apps.apple.com/us/app/simply-piano/id6503691129),
checked 2026-08-22.) JoyTunes' public GitHub repos
([github.com/joytunes](https://github.com/joytunes), checked 2026-08-22) — `mididriver`,
`Android-MIDI-API-backports`, `MIDI.js`, `GLNPianoView` — are all receive-side/rendering code,
nothing exposing an outbound light-guide protocol. No JoyTunes developer portal or SDK for
third-party hardware integration was found.

**Q2 — true hold duration?** Not applicable; no output protocol exists to characterize.

**Q3 — terms.** JoyTunes' Terms of Use
([hellosimply.com/legal/terms](https://www.hellosimply.com/legal/terms), effective May 20, 2025,
checked 2026-08-22), Section 3.1, prohibit reverse-engineering "[e]xcept where permitted by law
or relevant open source licenses" — a standard-form ban with a statutory-rights carve-out, not an
affirmative permission for third-party integration. Moot in practice since there is no output
channel to interoperate with.

**Conclusion:** Not a viable alternative target; purely a listening app with no lighting-output
feature of any kind.

## Piano Marvel

**Q1 — third-party light-guide output protocol?** No. Piano Marvel is architecturally a
MIDI-input grading app: it receives notes from a connected keyboard for accuracy/timing feedback
against sheet music, and draws its own on-screen feedback. Its own equipment and setup docs
describe only inbound MIDI —
[pianomarvel.com/en/article/learning-piano-on-a-midi-keyboard-what-you-need-to-know](https://pianomarvel.com/en/article/learning-piano-on-a-midi-keyboard-what-you-need-to-know)
and
[pianomarvel.com/en/equipment-materials](https://pianomarvel.com/en/equipment-materials) (both
checked 2026-08-22) — with no mention of a light-guide/key-lighting output feature anywhere in
those pages, the FAQ
([pianomarvel.com/en/faq](https://pianomarvel.com/en/faq), checked 2026-08-22), or the support
knowledge base.

**Q2 — true hold duration?** Not applicable; no output protocol exists to characterize.

**Q3 — terms.** Piano Marvel's Terms of Service
([pianomarvel.com/terms-of-service](https://pianomarvel.com/terms-of-service), checked
2026-08-22) explicitly bar exactly this kind of interoperability work, with **no carve-out** for
open-source or interoperability use:

> "Except as permitted by applicable law, decipher, decompile, disassemble, or reverse engineer
> any of the software comprising or in any way making up a part of the Site."

and, for the mobile app specifically:

> "Except as permitted by applicable law, decompile, reverse engineer, disassemble, attempt to
> derive the source code of, or decrypt the application."

The same terms also prohibit automated/bot access and systematic data collection ("use, launch,
develop, or distribute any automated system... that accesses the Site"; "systematically retrieve
data or other content from the Site... without written permission"). There is no separate
developer/API terms document. **An open-source project building against Piano Marvel's
software, were there anything to build against, would not be permitted under these terms**
without express written permission.

**Conclusion:** Not a viable alternative target; no output feature exists, and its terms would
block this kind of integration regardless.

## Yousician

**Q1 — third-party light-guide output protocol?** No public primary source found. Yousician's
Terms of Service only mention third-party software running *inside* the Yousician client (e.g.
bundled open-source libraries) — [yousician.com/terms-of-service](https://yousician.com/terms-of-service),
Section 4 "COMPATIBILITY," checked 2026-08-22 — with no mention of any hardware output protocol
or developer API. No Yousician-published developer portal, public API reference, or SDK for
third-party hardware/software integration was found (searched for `developer.yousician.com`,
`api.yousician.com`, and general "Yousician developer API" queries). Yousician's own hardware
buying-guide blog post
([yousician.com/blog/buying-guide-best-keyboard-for-beginners](https://yousician.com/blog/buying-guide-best-keyboard-for-beginners),
checked 2026-08-22) recommends seven ordinary MIDI keyboards with no LED/light-guide features and
describes no output protocol. Some retail/blog sources outside Yousician's own materials claim a
"Hi-Lite" light-up keyboard product works with Yousician, but since that claim does not trace to
anything Yousician itself has published, it is not treated as established here — **no public
primary source found** for any documented hardware-lighting partnership or protocol. (Yousician's
own support-site MIDI setup articles could not be fetched, blocked by a Cloudflare bot-challenge
at check time; this is noted as an access limitation, not a finding either way.)

**Q2 — true hold duration?** Not applicable; no output protocol was found to characterize.

**Q3 — terms.** Yousician's Terms of Service
([yousician.com/terms-of-service](https://yousician.com/terms-of-service), Section 10 "USE
RESTRICTIONS," checked 2026-08-22) broadly prohibit, with no interoperability carve-out: "unless
specifically authorized by law, attempt to reverse engineer, decompile, disassemble, or hack any
of the Services," and using "any automated system, cheats, spiders, hacks, scrapers, offline
readers or any unauthorized third party software designed to modify or interfere with the
Services." Section 4 further restricts the license to personal, non-commercial, executable-only
use, with no copying/modification "unless specifically permitted by Yousician." **Would not
permit this kind of integration**, though the point is moot in practice since Q1 found no output
protocol to build against.

**Conclusion:** Not a viable alternative target; no documented output feature, and terms that
would block the interoperability this project depends on for Synthesia.

## Melodics and ROLI (additional candidates)

These were checked as the closest real-world prior art to "a richer MIDI light-guide protocol
than Synthesia's," since both vendors are known to drive per-pad/per-key LEDs from software.

### Melodics

**Q1 — third-party light-guide output protocol?** No. Melodics' own support docs describe pad
lighting as tied to a **built-in "default mapping"** per supported device, not a generic protocol
a third-party device could independently target: "Manual mappings do not support pad lighting.
To restore pad lighting on supported instruments, use the default mapping... not all pad
controllers support pad lighting." —
[support.melodics.com/en/articles/6695766-connecting-your-pad-controller](https://support.melodics.com/en/articles/6695766-connecting-your-pad-controller),
checked 2026-08-22. The clearest evidence of a closed, per-vendor integration model: lighting a
ROLI Lightpad Block for Melodics requires opening **ROLI's own "ROLI Dashboard"** app and
selecting a dedicated Melodics preset ROLI itself ships — "Select the 'Apps' tab. Scroll down and
select the Melodics app. You should see a yellow 'M' appear on your Lightpad." —
[support.melodics.com/en/articles/7020565-troubleshooting-roli-lightpad-block](https://support.melodics.com/en/articles/7020565-troubleshooting-roli-lightpad-block),
checked 2026-08-22. Melodics' supported-hardware list
([support.melodics.com/en/articles/10763434-supported-instruments](https://support.melodics.com/en/articles/10763434-supported-instruments),
checked 2026-08-22) names hundreds of devices across Drums/Keys/Pads categories (Akai, Alesis,
Roland, Native Instruments, Novation, Pioneer DJ, and others) as "plug & play," but does not say
which of them support pad lighting specifically, or document any lighting protocol — only that
"all MIDI instruments can be mapped to Melodics" for note input via manual mapping. This is
per-vendor lighting profiles baked into each hardware maker's own control software (as the ROLI
Dashboard example shows), not an open, documented protocol.

**Q2 — true hold duration?** No public primary source found. Melodics publishes no MIDI
message-level technical spec for its lighting behavior in any of its support articles. Since
lighting rides on each hardware vendor's own default-mapping/firmware behavior (per Q1), whatever
note-hold-vs-strike-pulse timing actually occurs is presumably whichever vendor's own default
mapping implements — not something Melodics documents or controls end-to-end itself.

**Q3 — terms.** Melodics' Terms and Conditions
([melodics.com/legal](https://melodics.com/legal), checked 2026-08-22 — the live page renders via
JavaScript and would not return full text through automated fetching, so this is quoted as
surfaced by search-engine indexing of the page rather than a direct raw fetch) prohibit reverse
engineering, but narrowly scoped to *content*: "You will not extract or reverse engineer any of
the content contained within Melodics Apps, including but not limited to audio content, lesson
descriptions, artists images, or audio sequence information." No clause addressing third-party
hardware interoperability or connecting non-listed devices at the protocol level was found. An
open-source light-guide receiver wouldn't run afoul of a stated term here (there isn't one for
protocol-level interoperability), though it would need to avoid extracting Melodics' lesson/audio
content specifically. Moot in practice, since Q1 found no open protocol to build against.

### ROLI

**Q1 — third-party light-guide output protocol?** No — ROLI's lighting is a closed
hardware/software ecosystem, not an open output protocol. ROLI's own BLOCKS SDK
([github.com/WeAreROLI/BLOCKS-SDK](https://github.com/WeAreROLI/BLOCKS-SDK), checked 2026-08-22)
is, per its own README, a C++ SDK (also distributed via the JUCE framework) for building host
applications that discover and control ROLI's own BLOCKS hardware (Lightpad/Control/Seaboard
Block) — i.e. software that talks *to* ROLI's own hardware from a host machine, not a spec a
different vendor's keyboard could implement to interpret note-highlight instructions. ROLI's
Studio product page states its software instruments are "designed with Seaboard, ROLI Piano, and
Piano M in mind" and "pairs perfectly with other MPE controllers and works with standard MIDI
keyboards too" —
[roli.com/us/experience/roli-studio](https://roli.com/us/experience/roli-studio), quoted as
surfaced by search indexing, checked 2026-08-22 (see access-limitation note below) — which is
input/MIDI-note compatibility, not a documented output lighting standard. ROLI's own developer
BLOCKS FAQ could not be directly verified: `support.roli.com` requires an account login/OAuth
flow to view support articles, and ROLI's marketing pages
(`roli.com/us/legal/terms-of-service`, `roli.com/us/product/roli-studio`) were returning HTTP 402
Payment Required at check time — both blocked live access for this research, and an archive.org
fallback was also inaccessible from this environment. Findings for ROLI are therefore
cross-checked search-engine-indexed snippets of ROLI's own pages (traceable back to `roli.com`
URLs) rather than direct raw fetches; this limitation is noted rather than papered over.

**Q2 — true hold duration?** No public primary source found. No ROLI document accessible in this
research (BLOCKS SDK README, search-indexed snippets of the ToS/product pages) publishes
MIDI-message-level timing semantics for lighting/note-highlighting behavior.

**Q3 — terms.** ROLI's Terms of Use (`roli.com/us/legal/terms-of-service`, quoted via
search-engine indexing since the live page returned HTTP 402 and archive.org was unreachable from
this environment, checked 2026-08-22 — re-verification against the raw page is recommended when
it becomes reachable) are, per that indexed text, materially stricter than Melodics': Section 6.5
prohibits "disassembling, de-compiling, reverse engineering or creating derivative works based on
the whole or any part of the Platform, except to the extent permitted by applicable law"; Section
6.6 prohibits "accessing the Platform in connection with any non-authorized operating systems,
hardware or access points"; and Section 4.3 scopes the software license to use "in connection with
ROLI hardware devices ('ROLI Hardware'), or stand-alone on personal computer devices, as
applicable." Together these read as ROLI reserving the right to prohibit exactly this kind of
interoperability (reverse-engineering its protocol, or driving its software with non-ROLI
hardware), though Section 6.6's precise scope (jailbreak/OS-circumvention vs. third-party hardware
generally) isn't spelled out further in what could be recovered. The separately-licensed BLOCKS
SDK permits building software *for ROLI's own hardware*, not a license to reimplement ROLI's
lighting behavior for a different vendor's keyboard.

### Melodics/ROLI conclusion

Both are closed, per-vendor integrations, not a richer open protocol. The strongest concrete
evidence is Melodics' own instructions for lighting a ROLI Lightpad: it requires ROLI's own
Dashboard app and a vendor-shipped preset, not a documented message format Melodics or a third
party controls. There is no evidence either app solves the strike-pulse problem at the protocol
level; they simply light hardware that's individually pre-integrated with them, and neither
publishes MIDI-message-level timing semantics that would show a richer, hold-duration-aware
model even if one existed underneath.

## Recommendation

No app surveyed exposes an open, third-party-usable MIDI light-guide protocol that improves on
Synthesia's strike-pulse behavior. Ranked by how close each came to being a viable alternative:

1. **Melodics / ROLI** — the closest real-world precedent for "richer third-party hardware
   lighting," and still not a fit. Both drive lighting through closed, per-vendor integration
   presets (Melodics' own default-mapping list; ROLI's Dashboard app shipping a bespoke
   "Melodics" preset for its own Lightpad hardware; ROLI's BLOCKS SDK for building software that
   runs on ROLI's *own* hardware). Neither publishes any MIDI-message-level spec for its lighting
   behavior, so there's no public protocol to build against and no evidence of a richer,
   hold-duration-aware format underneath — the mechanism simply isn't documented either way.
   ROLI's terms are also the most explicit of anything surveyed about restricting use with
   non-ROLI hardware.
2. **Flowkey**: a real, working per-key lighting feature exists (Yamaha's "Stream Lights"), but
   it's a closed single-vendor hardware integration over a specific wired connection, undocumented
   at the protocol level, not usable by a third party at all.
3. **Simply Piano, Piano Marvel, Yousician**: no light-guide output feature exists in any of
   them; all are input-only note-recognition/grading apps, so there's nothing to interoperate
   with in the first place.
4. Where terms were checked, most explicitly prohibit reverse-engineering/unauthorized
   interoperation with no interoperability carve-out (Piano Marvel, Yousician, Flowkey, ROLI),
   JoyTunes/Simply Piano has a narrow statutory-rights exception only, and Melodics' restriction
   is scoped to content rather than protocol-level interoperation (though moot, since it has no
   open protocol to build against either).

**Synthesia's own feed remains the best available integration target for this project**, despite
its strike-pulse limitation. It's the only app surveyed that both actually publishes a
third-party-consumable light-guide output at all, and has terms (see the Synthesia baseline
section above) that are the least restrictive of everything checked here — no explicit
reverse-engineering prohibition was found in Synthesia's own published terms, unlike every
competitor's terms reviewed. Solving the hold-duration problem, if it's worth solving, means
working within Synthesia's feed (e.g. a heuristic for inferring intended hold duration from
note/measure data, if Synthesia exposes any) or requesting the feature from Synthesia directly,
not switching to a different practice app's protocol.
