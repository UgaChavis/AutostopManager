# Cold runtime binding

The optional `autostop.manager.cold-runtime-binding.v1` configuration binds a
new Manager process to one source revision, Linux boot, attempt and generation.
`epoch_id` equals the generation UUID. `AUTOSTOP_COLD_RUNTIME_BINDING` names the
root-owned immutable manifest and `AUTOSTOP_COLD_RUNTIME_BINDING_SHA256` pins its
bytes. A missing, replaced, writable, aliased, malformed or mismatched manifest
refuses startup. A physical cold source root also refuses an absent binding.
Ordinary source roots without either variable retain the previous behavior.

The native roles are `manager-mcp`, `j1`, `scheduler`, `work-telegram`,
`personal-telegram` and `wake`. The separate `crm-embedded` binding covers the
Manager library registered inside CRM; it is not a seventh native unit permit.
The epoch document must bind all seven manifests. All paths are exact derived
paths under `/etc/autostop-manager-cold/<epoch>/<role>`,
`/var/lib/autostop-manager-cold/<epoch>/<role>` and
`/run/autostop-manager-cold/<epoch>/<role>`. The source is the physical
`/opt/autostop-manager-cold-releases/<revision>/source` with a matching root-owned
`REVISION`. `cold_runtime.expected_paths` is the canonical per-role field map.
Cross-role sockets and the J1 VIN scratch path derive from the same epoch.

Consumers resolve the binding before Telegram owner/account constants, default
arguments and Wake configuration targets are established. Conflicting legacy
environment paths are rejected. Cold J1 workers refuse prior running rows
instead of requeueing them. Cold schedulers cannot adopt or control the legacy
system timers. Legacy Telegram job import is unavailable in a cold slot. These
rules do not migrate any history, sessions, credentials, pending jobs or cursors;
the separate release controller must provide reviewed fresh state and retain
unknown historical operations for review.

A manifest grants no dispatch permission. `dispatch_guard(operation_id,
channel, payload_sha256=None)` opens the existing root-owned shared lock and
holds a shared `flock` across the actual consumer. The descriptor pins device,
inode and ctime; the controller must create and finalize permissions before
sealing it, then use an exclusive lock on that same inode for opening/closing.
It must never replace or recreate the lock. Missing/busy/replaced locks refuse
admission. The immutable fence descriptor points to a mutable exact epoch state;
closed, missing or malformed state refuses dispatch.

An open state must reference the exact cold plan and committed history record.
The typed cold plan binds the attempt, candidate Manager SHA and all epoch
bindings; its canonical SHA must match the COMMIT's embedded plan. The current
protected HEAD must name that record and sequence. A warm plan or a copied
standalone receipt cannot open dispatch. The separate controller remains
responsible for validating the complete release history, real native acceptance
and all required release proofs before it publishes this open state.

The immutable tombstone catalog binds the same attempt/boot/generation/epoch.
Any matching old `op_id` is forbidden, including with another payload digest.
Missing identity refuses dispatch; the guard does not generate replacement UUIDs
or replay an operation. Existing Store/owner HTTP writes, Manager CRM writes,
Telegram sends, scheduler jobs/outbox, research workers and Wake turn starts use
their domain request/job/event identities at the consumer seam. Their existing
business, idempotency, revision and readback checks continue to apply.

Admission is distinct from the result. An exception, cancellation or lost reply
never proves that a dispatched remote operation did not happen. The guard does
not retry, cancel remote work, clear unknown outcomes, release the old hold or
turn a file lock into proof of empty external queues. Recovery must preserve the
domain receipt/unknown state and reconcile it independently.

Manager MCP uses loopback HTTP, not a Unix socket. Its new port/path must be
explicitly configured in the pinned role environment and independently measured.
The cold Wake endpoint needs its own admitted app-server socket and owned thread;
this source change does not create either. The CRM container must require the
`crm-embedded` manifest before importing its app, mount the exact cold Manager
source and protected dispatch references, and join the same serialized epoch.
Six native slot permits alone do not cover that embedded writer.

This change creates no runtime files, systemd units, manifests, credentials,
holds, migrations or live permission. It is source support for a separately
reviewed cold release. Tests use synthetic documents and disposable file locks;
they do not provide native or production acceptance.
