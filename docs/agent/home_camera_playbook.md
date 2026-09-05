# Private Home Camera

Use only for an explicit owner-requested photo, short silent clip or bounded PTZ
action. Do not turn it into scheduling, monitoring, audio, archive, continuous
tracking or person identification. One request may include a bounded vehicle
search.

The route is Manager → localhost SSH forward → `home-pc` → private RTSP/ONVIF.
Root-owned `/etc/autostop-camera/home-tapo-c225.json` owns configuration; inspect
only metadata and validation results. Use the project helpers:

```bash
.venv/bin/python scripts/capture_home_camera.py --mode photo --stream high --output <private.jpg>
.venv/bin/python scripts/capture_home_camera.py --mode clip --duration 10 --output <private.mp4>
.venv/bin/python scripts/control_home_camera_ptz.py --action status
.venv/bin/python scripts/control_home_camera_ptz.py --action move --direction right --step small
```

Work in a private root-owned output directory and do not overwrite by accident.
Verify `home-pc` through `codex_home_pc_reverse_ssh.md`, inspect the produced
media, and ensure the SSH process closes. For PTZ, learn the initial position,
use bounded moves with observations, restore it and verify; stop rather than
guess after an uncertain move.

Deliver only the requested artifact. Media, room/vehicle details, camera state
and credentials remain transient and never enter Git, docs or Manager memory.
