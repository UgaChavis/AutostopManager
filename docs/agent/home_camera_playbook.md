# Private Home Camera

Canonical route for owner-requested capture or bounded pan/tilt from the
private Tapo home camera. This route is separate from the public
`Семафорная 185` traffic-camera workflow.

## Boundaries

- Capture or move only after an explicit owner request. Do not schedule, poll,
  archive, identify people, track objects continuously, record audio, or run
  background monitoring. One request may authorize one bounded vehicle search.
- Never publish RTSP or ONVIF. The path is Manager -> temporary localhost-only
  SSH forward -> `home-pc` -> private camera RTSP/ONVIF.
- Never print, log, document, commit, or persist camera credentials outside the
  root-only runtime configuration.
- The home PC and camera must be powered on, Privacy Mode must be off, and the
  camera address must remain in AmneziaVPN's exact-IP bypass list.

## Runtime Configuration

The configuration is `/etc/autostop-camera/home-tapo-c225.json`. Require the
directory to be root-owned mode `0700` and the file root-owned mode `0600`.
Inspect only ownership, mode, required-key presence, and validation booleans;
never display its contents.

The helpers validate the private IPv4 target, fixed services, SSH alias, and
expected Windows hostname. RTSP opens through PyAV with TCP transport. PTZ
uses ONVIF Profile S with WS-Security PasswordDigest. Credentials remain only
in process memory and never enter subprocess arguments or output JSON.

## Commands

```bash
.venv/bin/python scripts/capture_home_camera.py \
  --mode photo --stream high --output /exact/private/frame.jpg

.venv/bin/python scripts/capture_home_camera.py \
  --mode clip --duration 10 --output /exact/private/clip.mp4

.venv/bin/python scripts/control_home_camera_ptz.py --action status
.venv/bin/python scripts/control_home_camera_ptz.py \
  --action move --direction right --step small
.venv/bin/python scripts/control_home_camera_ptz.py \
  --action goto --pan START_PAN --tilt START_TILT
.venv/bin/python scripts/control_home_camera_ptz.py --action stop
```

Photo capture uses the high stream first and retries the low stream once only
after a technical failure, never after authentication failure. Clips are
video-only and limited to 1–30 seconds. Existing output files are not replaced
unless `--overwrite` is explicit. The output parent must be a real root-owned
private directory with mode `0700`; the helper writes only to a hidden mode
`0600` staging file, validates it, then publishes it atomically. `--overwrite`
therefore leaves any prior artifact untouched until validation succeeds,
including when the requested final leaf is a link. On failure it removes only
the staged partial file.

## Bounded PTZ And Vehicle Search

- PTZ exposes only `status`, allowlisted relative `move`, bounded `goto`, and
  emergency `stop`. Relative steps are `small=0.05` and `medium=0.10`; never
  use continuous movement, arbitrary SOAP, patrol, tracking, or more than five
  moves in one owner request.
- Start with `status` and keep its pan/tilt only in transient task context.
  Close the ONVIF tunnel before opening RTSP: this home-PC route must use only
  one camera tunnel at a time. Capture the initial view, move one step, close
  ONVIF, capture the next view, and repeat. On completion use `goto` with the
  initial position and capture once more to verify restoration.
- If a move or restore fails, run `stop`, return a safe error, and do not guess
  the camera position. The lock allows only one PTZ controller at a time.
- For an explicitly requested vehicle search, inspect only the minimum sector
  frames needed and report `found`, `not_found`, or `insufficient_view` with a
  confidence note. Keep vehicle description, plate, frames, and room view
  transient; never identify people or write them to Manager memory.

## Verification

Before capture, verify exact `home-pc` identity and strict host-key handling.
If the loopback reverse listener exists but SSH times out during banner
exchange, resolve its exact `sshd: codex-home-tunnel` child and root `[priv]`
parent, terminate only that stale server-side connection once, wait for the
scheduled Windows client to reconnect, and recheck the expected hostname.
Never rotate keys or accept a new host key as outage recovery.
After capture, require safe JSON success, JPEG or MP4 validation with
`ffprobe`, expected dimensions, mode `0600`, and cleanup of the SSH process.
After any Windows networking work, verify both AmneziaVPN and reverse SSH.

Deliver only the requested artifact. Keep no Manager memory, documentation, or
Git copy of a frame, clip, face, room view, address, username, or password.
