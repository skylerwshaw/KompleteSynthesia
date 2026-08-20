# Capturing the MK3 Screen template frame

The MK3 Screen feature displays an image by replaying a real `client_parameter_page_set_data`
frame captured from Komplete Kontrol, substituting only the client UUID and the 32-byte
asset handle (see `docs/adr/0001-mk3-screen-over-odr.md` for why replay instead of
synthesis). This is the one step that needs the physical MK3, and it is the operator's to
run. It captures Komplete Kontrol's own traffic; it drives nothing on the keyboard itself.

## Prerequisites

- An MK3 connected, with NI's `NIHardwareConnectionService` running.
- Komplete Kontrol installed.
- `python3` with `msgpack` (`pip3 install msgpack`).

## Steps

1. **Quit Komplete Kontrol and KompleteSynthesia** (and any other ODR client). An
   already-open connection bypasses the relay, so clients must reconnect through it.

2. Start the recorder:

   ```
   ./scripts/odr_capture.py --out captures/mk3-template
   ```

   It renames the ODR socket aside and listens in its place. On Ctrl-C (or any exit) it
   restores the original socket automatically.

3. **Launch Komplete Kontrol** and load a preset whose parameter page carries a **background
   image but an empty or minimal parameter list**. The empty parameters matter: the replay
   keeps everything in the captured frame except the two substituted spans, so any macro
   labels present here would render under our images later (the "wart" we are avoiding).
   Navigate so the parameter page with its header image is shown, which is what makes the
   service send a `client_parameter_page_set_data` frame carrying a background handle.

4. **Ctrl-C** to stop. Watch for `restored original socket path`.

## Verify the capture

```
grep -c 'notify client_parameter_page_set_data' captures/mk3-template/journal.jsonl   # >= 1
ls -la captures/mk3-template/raw/conn01-c2s.bin                                        # the raw client->server stream
```

You want at least one `client_parameter_page_set_data` frame, and it must reference a
32-byte background handle. If none appears, the page never showed a background image;
try a different preset/page.

## What happens next

Hand the `captures/mk3-template/` directory back. Step 5 wires `ODRClient` to load that one
frame's raw bytes and replay it with our client UUID and the uploaded asset handle
substituted in, which is what actually puts the rendered note band on the panel. The
capture is a build input, not something we commit as-is (`captures/` stays out of git).
