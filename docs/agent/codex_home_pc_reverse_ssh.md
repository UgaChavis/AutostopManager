# Remote workstation access

Use this route only when the owner explicitly asks to work on the private
workstation. The tracked repository deliberately contains no host name, user,
port, public address, key path, task name, software-version inventory, or
credential location.

## Source of truth

- The server-local SSH configuration is the runtime source of truth for the
  `home-pc` alias.
- Optional private operating notes belong under
  `data/private_knowledge/remote_access.json`; `data/` is ignored by Git.
- Never copy resolved topology or command output back into tracked docs,
  Manager memory, run summaries, or chat reports.

## Safe workflow

1. Confirm that the request explicitly targets the private workstation.
2. Inspect the alias locally with `ssh -G home-pc`; do not print the result.
3. Run a bounded, non-mutating connection check with `ssh -o BatchMode=yes
   home-pc 'echo REMOTE_OK'`.
4. Use `ssh`, `sftp`, or `scp` only for the requested target and operation.
5. Before a write, read the exact target state, create a local backup when
   applicable, make the smallest change, then reread and verify it.
6. Keep passwords, private keys, tokens, personal files, host inventory, and
   tunnel logs out of Git and Manager memory.

If the alias is absent or the check fails, stop after local diagnostics and
report the single missing runtime prerequisite. Do not recreate accounts,
rotate keys, open firewall ports, or expose a public SSH listener without a
separate exact owner command.

## Provisioning and rollback

`scripts/codex_home_pc_bootstrap.ps1` is a parameter-only template. Supply all
topology values at runtime from a private channel; do not put them into a
tracked wrapper or command transcript. Before provisioning, save the existing
SSH configuration, firewall rule set, scheduled-task definition, and key
fingerprints outside Git. Rollback must restore that snapshot rather than use
hard-coded user, port, or task deletion commands.
