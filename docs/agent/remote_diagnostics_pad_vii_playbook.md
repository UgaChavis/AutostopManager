# Launch PAD VII: operator route

This is the Manager-side entrypoint for supervised remote diagnostics. The
canonical server contract is
`/opt/autostop-remote-diagnostics-server/docs/REMOTE_DIAGNOSTICS_PAD_VII.md`;
read it in full before any live tablet call. It owns the wire details and must
not be copied here.

The only supported action plane is the project-scoped
`autostop_remote_diagnostics` stdio MCP. It starts the fixed root-owned
`/usr/local/libexec/autostop-remote-staging-mcp` launcher, which runs the
adapter as the staging identity. Manager config and Git contain no tablet ID,
UDS, pairing, TLS or runtime-environment values. Do not proxy these tools
through the Manager/CRM MCP server, make the UDS readable, or add ADB, shell,
raw Intent, UIAutomator, queues or retries.

## Session gate

- Do not call `device_status`, `observe`, `history` or any action until the
  owner explicitly starts the live session.
- After the last reconnect wait for metrics-confirmed authenticated READY, then
  obtain one current cached status where all of these hold together:
  `connected=true`, `ready=true`, `mode=CONTROL`, `controlEnabled=true`,
  `screenState=onUnlocked`, `mediaProjectionActive=true`,
  `accessibilityEnabled=true`, `foregroundKind=launch` and
  `commandAvailable=true`.
- `foregroundKind=self` is a wait state: the owner opens Launch manually; do
  not use `open_launch`. This tool is deliberately not enabled.
- Use only `fresh observe → at most one action → fresh observe`. Screenshot is
  opt-in for an ambiguous UI; a swipe success is dispatch only.

## Read-only next session

Use the reviewed scenario: vehicle identity → system scan → module list → DTC
→ freeze-frame → live data → compact structured summary. Obtain a fresh observe
at every screen transition. Clear/erase DTC, active tests, resets/service
functions, coding, adaptation, calibration, flashing, immobilizer work and
unknown screens are stop conditions pending exact owner authorization.

Keep UI trees, screenshots, VINs, diagnostic values, observation IDs and action
payloads transient. The final report may retain only the owner-approved concise
conclusions and limitations.
