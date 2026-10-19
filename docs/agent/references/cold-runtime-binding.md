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

The six native policies use the exact candidate venv interpreter with
`-I -B <source>/scripts/cold-role-start.py --role <role>`. This fixed source
entrypoint requires the binding and role before importing the application, then
selects the existing module and arguments. It adds only its own physical cold
source root; dependencies load from that candidate venv's normal site. It does
not use `PYTHONPATH`, a legacy editable install or a caller-selected module.
The separate native guard must pin the interpreter, bootstrap and reviewed source
before execution admission. The script and manifest do not grant native access.

Consumers resolve the binding before Telegram owner/account constants, default
arguments and Wake configuration targets are established. Conflicting legacy
environment paths are rejected. Cold J1 workers refuse prior running rows
instead of requeueing them. Cold schedulers cannot adopt or control the legacy
system timers. Legacy Telegram job import is unavailable in a cold slot. These
slots cannot inspect or call the legacy J1 browser; that role remains denied.
Unit-name/native health for the six new slots belongs to the controller's exact
epoch slot mapping, not to hardcoded legacy unit names or this path binding.
These
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

An open state references the exact source plan/COMMIT and an immutable admission
projection under `dispatch/proofs`. The projection contains canonical copies of
the plan, COMMIT, HEAD and epoch, with equal hashes for each source/projected
reference. It binds the same attempt, boot, generation and seven manifests. A
warm plan or an unrelated standalone receipt cannot open dispatch. Workers read
only this bounded technical projection; private controller journals keep their
permissions and are not mounted into CRM.

The controller validates the actual private history/current HEAD, native
acceptance and release proofs under the exclusive epoch lock, publishes readable
root-owned projection files, then opens admission. The cold state store must
close that epoch under the same exclusive lock before any later HEAD/pending
mutation. A worker verifies this published admission cut; it does not independently
probe the private host HEAD. Every admitted controller must enforce that close
ordering. A projection alone does not grant permission or prove current runtime.

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

Telegram also needs separately staged, verified authentication material before
native readiness can pass. An empty session requires an authorized login; this
change does not perform one. A session copy must be separate and writable by its
new slot, with an explicit allowlist of authentication material. Do not copy old
inbound/outbox/idempotency data, queued updates, entity caches or update cursors
as part of that step. Session setup is not proof of an empty provider backlog;
intake needs its own accepted boundary. No client messages are sent by preparation.

This change creates no runtime files, systemd units, manifests, credentials,
holds, migrations or live permission. It is source support for a separately
reviewed cold release. Tests use synthetic documents and disposable file locks;
they do not provide native or production acceptance.

The manual `six-role-runtime` GitHub workflow prepares the declared core and
`telegram` extras for exact CPython 3.12.15 on Linux x86_64. It records the source
wheel, every dependency wheel, versions and SHA256 hashes, then installs the
frozen lock into a new venv without an index or network namespace connectivity.
It checks dependency imports for all six role entrypoints without starting a
role, loading authentication/sessions or a Whisper model, or contacting an API.
The workflow runs only on a fresh hosted runner and publishes an artifact only.

The artifact's installed-file inventory describes its disposable verification
venv. It supplies unique hash/byte totals and dependency provenance; the release
producer must still verify the actual candidate prefix, physical Python base,
startup index and loaded native libraries. A core-only wheelhouse omits Telethon
and faster-whisper and cannot satisfy this six-role dependency gate.
