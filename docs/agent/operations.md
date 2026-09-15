# Operations reference

Use only the relevant section. Shared scope/privacy rules live in [manager_rules.json](manager_rules.json).

## CRM cleanup

For a requested cleanup, use `agent_board_workflow(operation="cleanup_card")` with
the current card, action contract, revision and idempotency. Patch only unclear
facts and next actions; preserve history, manual diagnosis and unrelated fields.
Resolve client/vehicle links through their dedicated actions. Moving, archiving,
deadlines and finance need their own authorization. Reread the changed card.

## Documents and Gmail

Use CRM printing and AutoStop templates for CRM documents, not parallel PDF/HTML.
Manual acts use `save_completion_act_form`; resetting needs separate permission
and a recovery snapshot. Confirm current requisites and applicable official form
requirements; old documents are not proof of current details. Check calculations
and rendered output before delivery, not just extracted text.

Use the active Gmail connector for the requested scope; inspect attachment metadata
before downloading. Confirm the recipient and artifact before sending. For requested
mail cleanup, archive to retain mail; Trash only when deletion was requested.

## Remote access

Resolve one authorized target; `ssh -G <alias>` shows configuration, not reachability.
Stop on an unexpected host, user or key; recovery and key changes need separate scope.

- Main VPS: `autostop-vps27560`; no silent fallback alias.
- Managed PCs/printing: `/opt/autostop-managed-pc/README.md`, then one exact alias.
- FST.KZ: follow [AGENTS.md](../../AGENTS.md); never reuse home-PC credentials.
- Legacy `home-pc`: verify hostname `DESKTOP-BUSO4I8`. Bootstrap is a separate
  recovery action; verify its `ServerHost`, never infer access from old instructions.

## Private home camera

Only an owner-requested photo, short silent clip or bounded PTZ action; no monitoring,
audio, archive, tracking or identification. Use `scripts/capture_home_camera.py` or
`scripts/control_home_camera_ptz.py`; they own the temporary `home-pc` SSH forward.
Inspect only configuration metadata/validation, not its credentials. Use a new private
output path, inspect and deliver the requested artifact. For PTZ, observe the starting
position, make small verified moves and restore it; stop on uncertainty. Keep media temporary.
