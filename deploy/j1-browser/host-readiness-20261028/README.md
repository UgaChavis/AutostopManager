# J1 browser: host configuration, 2026-10-28

This is a host configuration package for the installed Manager13e8b829 and its
qualified Chromium156 renderer/proxy images. It does not replace the Manager
snapshot, modify the eight packaged Python modules, change the CRM catalog pin,
or rebuild images. The startup wrapper resolves the sealed current revision
through the native stack runner before starting that revision with `--no-build`.

Install as root. The host configuration directory is
`/etc/autostop-j1-browser/20261028`, owned by root with mode0700.

| File | Destination | Mode |
| --- | --- | --- |
| `chromium-launcher.sh` | `/etc/autostop-j1-browser/20261028/chromium-launcher.sh` | 0755 |
| `run-stack.sh` | `/etc/autostop-j1-browser/20261028/run-stack.sh` | 0700 |
| `resource-overlay.yaml` | `/etc/autostop-j1-browser/20261028/resource-overlay.yaml` | 0600 |
| `zzzzzz-readiness-resources-20261028.conf` | `/etc/systemd/system/autostop-j1-browser.service.d/zzzzzz-readiness-resources-20261028.conf` | 0644 |
| `zzzzzz-browser-ipc-20261028.conf` | `/etc/systemd/system/autostop-j1.service.d/zzzzzz-browser-ipc-20261028.conf` | 0644 |

The Compose overlay adds one read-only launcher mount and the corresponding
binary path. All other native container settings remain unchanged: UID10001,
read-only rootfs, dropped capabilities, fixed proxy, private control network,
renderer128PIDs/768MiB and proxy64PIDs/256MiB. The launcher limits Chromium
fanout with four fixed resource flags, serializes browser processes with a
15-second lock wait and retains a0.2-second child drain. Version checks cache
only a successful direct binary result in the container tmpfs, directory0700
and atomic file0600; the read-only binary cannot change within that container.

The browser drop-in replaces only `ExecStart`. The native `ExecStartPost`
attester and `ExecStopPost` cleanup remain in force. Never create an isolation
marker manually. After daemon reload, enable/start the browser service, then
run the native installer with `--verify` and native MCP `--browser-check`.
Verify labels, source hashes, topology, DNS guard, sealed socket/marker and
actual rendered output. Complex pages may reach the unchanged resource limit;
bounded failures remain part of the browser contract.

The J1 worker retains UID0 for the root-only IPC contract and adds only
`CAP_DAC_OVERRIDE` to its existing SETUID/SETGID capability sets. Its mount
namespace, private directories, `NoNewPrivileges` and parser privilege drop
remain unchanged. Before its graceful restart, verify through a read-only DB
connection that there are no running/queued jobs and prevent forced SIGKILL
with a temporary stop-only override. Remove that temporary override before
starting the worker. Verify actual worker capabilities, socket access and
guarded J1 browser fallback in the same namespace. No Telegram/MCP restart,
client message, CRM/Store business write or old-event replay is required.
