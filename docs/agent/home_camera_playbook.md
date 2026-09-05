# Private Home Camera

Use only for an owner-requested photo, short silent clip or bounded PTZ action.
It is not monitoring, audio, archive, tracking or person identification.

Use `scripts/capture_home_camera.py` and `scripts/control_home_camera_ptz.py`;
choose only the parameters the request needs. They handle the normal temporary
localhost SSH forward through `home-pc`. The root-owned configuration is not
content to inspect; use only its metadata and validation results. Consult
`codex_home_pc_reverse_ssh.md` only when access needs diagnosis.

Write to an exact, private root-owned output path without overwriting an existing
artifact, then inspect the result. For PTZ, establish the initial position, make
small observed moves, restore it when changed, and stop on uncertainty.

Deliver only the requested artifact. Media, scene details, camera state and
credentials stay transient and never enter Git, docs or Manager memory.
