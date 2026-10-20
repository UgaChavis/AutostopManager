# Fixed component admission (draft, not a production permit)

This contract adds two explicit modes to the existing protected cold paths and
dispatch flock. Existing `autostop.manager.cold-runtime-binding.v1`, its seven
roles, CRM HEAD/projection checks and legacy absence behavior are unchanged.
There is no component fallback when a binding is absent.

* `manager_external`: exactly manager-mcp, j1, scheduler, work-telegram,
  personal-telegram and wake. The current CRM container/app/mount/env and its
  embedded Manager89 remain outside this component's retired actor set.
* `crm`: exactly seven roles, bound to a candidate CRM revision and the durable
  initial external component commit. Native roles keep the same Manager source
  and artifact, with new configuration/instances; crm-embedded uses the CRM
  image's own qualified physical interpreter. All use the existing independent
  cold Board/MCP ingress prefixes.

All UUIDs are canonical; hashes are lowercase SHA256 (revision fields Git40).
JSON is canonical sorted compact UTF8 with a final LF. Fields below are exact;
booleans are not integers. The monotonic startup interval is half-open and at
most 600 seconds, on the bound boot. Its expiry cannot authorize a fresh restart.
An already committed OPEN runtime remains valid after the startup interval;
every effect still reads current protected state under the same shared flock.

## Names and paths

`C = /var/lib/autostop-component-release/<attempt>` is controller-private 0700.
`P = /var/lib/autostop-manager-cold/<epoch>/dispatch/proofs` holds only the small
root-owned, role-readable byte-identical authority projections. Claim/config
files are role-readable root-owned files under the existing epoch config root.
Workers never need read access to controller-private evidence or secret inputs.

The source and native runtime are exactly
`/opt/autostop-manager-cold-releases/<ManagerGit40>/{source,venv}`. All seven
existing role path definitions remain unchanged. No old path/queue fallback.

## Binding

`/etc/autostop-manager-cold/<epoch>/<role>/binding.json`:

* schema `autostop.manager.component-runtime-binding.v1`
* mode, source_revision, attempt_id, boot_id, generation, epoch_id, role, paths,
  tombstones_ref, ingress_fence_ref (the existing exact paths/ref grammar)
* instance_id (one launch UUID), runtime, claim_ref, not_before_ns, expires_at_ns
* manager_external only: retained_crm_ref (C/retained-crm.json)
* crm only: external_commit_ref (the prior C/commit.json), candidate_crm_sha

`runtime` is exactly `{path,sha256,version}`. Version is the full nine integers
`[dev,ino,size,mtime_ns,ctime_ns,mode,uid,gid,nlink]`. A native runtime path is
exactly `<release>/venv/bin/python3.12`; only crm-embedded uses the image's own
protected physical interpreter. The qualified controller/bootstrap hashes actual runtime
bytes; the consumer checks its actual executable path and every version field
against that immutable qualification, without a mutable runtime cache.

`claim_ref` points to `/etc/autostop-manager-cold/<epoch>/<role>/claim.json`.
That record has exactly schema `autostop.manager.component-claim.v1`, mode,
attempt_id, boot_id, generation, epoch_id, source_revision, role, instance_id,
runtime, pid, start_ticks, pid_namespace (inode), claimed_ns. The root controller
must consume a per-role launch reservation once (durable O_EXCL ledger) before
publishing this record, with the actual bounded bootstrap PID/birth/namespace.
No application import, UID drop or child is allowed before this claim. A partial
or unknown claim cannot be removed and retried as the same launch.

`cold-role-start.py` validates the same-process claim and the fresh interval
before importing any application module. Ordinary bound config/effect readers
also require that actual PID/birth/namespace/runtime claim. A new child cannot
reuse its parent's binding. The library only reads authority; nonprivileged
Telegram and CRM consumers never write controller state.

## Admission and status

Root C/admission.json and its byte-identical P/admission.json have exactly:

* schema `autostop.manager.component-admission.v1`, mode, attempt_id, boot_id,
  generation, epoch_id, source_revision, not_before_ns, expires_at_ns
* runtime_bindings: canonical binding refs, exactly the mode's six or seven roles
* crm_urls: exactly `{board,mcp}`, canonical explicit loopback8000/8001 URLs
* ownership_ref: P/ownership.json
* manager_external: retained_crm_ref, equal to every binding's C reference
* crm: external_commit_ref and candidate_crm_sha, equal to the binding

Manager-external URLs are existing root Board and `/mcp` routes. CRM-mode URLs
are `/__cold/<Board64hex>` and `/mcp/__cold/<MCP64hex>/mcp`; their nonces are
independent. Configured URLs must equal the protected admission exactly. No
redirect, remote host, arbitrary path or guessed Board nonce is accepted.

The state at the existing `dispatch/state.json` has exactly schema
`autostop.manager.component-dispatch-state.v1`, mode, attempt_id, boot_id,
generation, epoch_id, state (`closed|open`), admission_ref (P/admission.json),
commit_ref (null or P/commit.json), store_effects_ref (null or P/store-effects.json).
The fixed existing fence/lock descriptor and full inode identity are unchanged.
Start requires CLOSED and no commit. Root publishes OPEN only after actual
component readiness and local backup-copy verification, preserving the old
unit/hold barriers. Any unknown, failed admission or wrong binding stays CLOSED.

C/commit.json and P/commit.json have exactly schema
`autostop.manager.component-commit.v1`, mode, attempt_id, boot_id, generation,
epoch_id, source_revision, admission_ref (original C/admission.json), claim_refs
(exactly six or seven), instances (the same exact role set), committed_ns. Each
instance has exactly instance_id, pid, start_ticks, pid_namespace and
runtime_sha256, derived from its actual immutable claim. UUIDs and
`(pid_namespace,pid)` pairs must be distinct; each consumer checks its own row
against its current process. Commit time is within the original interval.
The root coordinator must check a fresh clock after final publication I/O and
close under EX on expiry/failure. No serialized receipt extends that deadline.
CRM-mode also reads P/external-commit.json, byte-identical to the explicitly
bound earlier external commit, with the same boot and Manager source revision.
It is not an authorization to adopt unknown or stopped external instances.

## Retained components and ownership

C/retained-crm.json and P/retained-crm.json have exactly schema
`autostop.manager.component-retained-crm.v1`, mode, attempt_id, boot_id,
generation, epoch_id, container_id, image_id, app_revision, embedded_revision
(exact89af29082e0a6f72c5d85ae448acb6d4deec5d94), manager_source_root,
manager_source_sha256, manager_mount, environment_sha256, owned_paths.
manager_mount has exactly source, destination (`/opt/AutostopManager`), read_only
(true), data_source, data_destination (`/opt/AutostopManager/data`). owned_paths
is a nonempty sorted unique bounded list including data_source, disjoint from
new component state. Actual container/image/mount/env and own queues are freshly
measured by the controller; this document does not declare old CRM stopped.

P/ownership.json has exactly schema `autostop.manager.component-ownership.v1`,
mode, attempt_id, boot_id, generation, epoch_id, source_revision, roles (exact
mode role set), original_units_denied (all eight original fixed unit names),
hold_ref (C/hold.json), retirement_ref (C/retirement.json), retained_operator_ref
(null or C/operator.json), old_writer_allowed(false), candidate_may_have_written
(true). The writer flags apply to the retired component actors, not retained
CRM/Store or a separately qualified administrative operator. Evidence refs are
private metadata links, not caller `verified=true` permissions.

The current operator may belong to the original unparsed kernel actor scope.
Never use the old retire-all helper on it. An operator exemption needs its exact
PID/birth/source and actual closure of old automated business transports, fresh
new worker sockets/HOME and no old state adoption. Unknown ingress/inflight is
STOP; use a reviewed local one-shot handoff executor if safe separation cannot
be proved. This contract does not claim the old kernel retired or quiescent.

## Store writes

GET/read paths are unchanged. Every bound Store action/owner mutation needs a
separate reviewed P/store-effects.json; null/unknown is DENY before HTTP.
The record has exactly schema `autostop.manager.component-store-effects.v1`,
mode, attempt_id, boot_id, generation, epoch_id, source_revision, admission_ref
(original C reference), allowed_operations (`manager-store` and
`manager-store-owner`: sorted unique exact operation names), payment_scope_ref
(null or P/store-scope.json). No wildcard or legacy/owner credential fallback.

`set_order_payment_status` and generic owner mutations additionally require
P/store-scope.json with exactly schema `autostop.manager.component-store-scope.v1`,
mode, attempt_id, boot_id, generation, epoch_id, source_revision, store_revision,
principal_id (the preserved UUID), scope (`store:write:order_payment`),
amendment_receipt_ref (the canonical private Store-stage receipt). The
Store-stage controller must produce it only from actual same-principal scope
readback. It qualifies the immutable receipt -> intent -> snapshot chain, exact
Store54/manage principal, production_native runtime identity and the five true
fields ok, commit_acknowledged, independent_readback, runtime_continuity and
credential_metadata_preserved. Fixture receipts cannot grant production use;
naming a scope in local configuration never repairs the Store DB.
The existing operation identity/tombstones, single POST attempt, expected-state
checks and independent response readback remain required.

## Fixed root launch and role configuration

The exact native unit is
`autostop-manager-component-<generation>-<role>.service`, directly under
`/system.slice`, with root UID/GID for its bootstrap. It starts only:

```
<release>/venv/bin/python3.12 -I -S -B <release>/source/scripts/component-role-bootstrap.py --role <role> --launch C/launch/<role>.json --sha256 <launch-sha256>
```

`C/launch/<role>.json` has exactly schema
autostop.manager.component-launch.v1, mode, source_revision, attempt_id, boot_id,
generation, epoch_id, role, instance_id, runtime, identity, source_manifest_ref,
not_before_ns, expires_at_ns and binding_template. Identity is exactly
`{uid,gid,supplementary_groups}`. Binding_template is the complete component
binding without claim_ref; its common fields must equal the launch. The source
manifest is `<release>/source/MANIFEST.sha256`, with the full regular source
inventory except itself, including REVISION and both fixed startup scripts.
Every selected/unselected member, physical path, protected owner/mode and hash
is checked before any project import, then every source version is rechecked
before dropping privileges. Native runtime prefix must contain the final new
Manager revision; an earlier capsule cannot be relabelled or rewritten.

The initial typed dispatch state is CLOSED with admission_ref, commit_ref and
store_effects_ref all null. Under its existing EX flock the bootstrap consumes
`C/claims/<role>.json` with O_EXCL first, then writes root-owned role-readable
claim.json and binding.json. The private launch-claim ledger has exactly schema
autostop.manager.component-launch-claim.v1, mode, attempt_id, boot_id, generation,
epoch_id, role, instance_id, launch_ref, claim_ref, binding_ref, pid, start_ticks
and pid_namespace. Interrupted publication retains its consumed ledger. It
cannot be deleted/retried or replaced by a second child.

The bootstrap releases EX and waits only within the original interval for the
coordinator's nonnull admission_ref. Under SH it checks admission/CLOSED, source
versions and revocation, drops to the fixed identities below, then execs the
same physical interpreter with `-I -B scripts/cold-role-start.py --role <role>`.
The same PID/birth/namespace is required. The role entrypoint rechecks its claim
and loads the exact protected runtime.env before any application module import.

| Role | UID | GID | Supplementary groups |
| --- | ---: | ---: | --- |
| manager-mcp | 0 | 0 | [] |
| j1 | 0 | 0 | [] |
| scheduler | 0 | 10001 | [10001] |
| work-telegram | 987 | 982 | [982,10001] |
| personal-telegram | 993 | 985 | [985] |
| wake | 0 | 982 | [982] |

Use Type=simple/exec and Restart=no for this one-shot admission, not ambient
Type=notify readiness: the clean bootstrap does not forward NOTIFY_SOCKET.
Do not add another ExecStartPre/app process or automatic restart after an
unknown/expired launch. Retain existing hardening where compatible with the
fixed epoch paths and exact cgroup. Service-specific write paths are only the
existing role state/run roots, with reviewed cross-role socket access.

Bootstrap supplies a fixed clean PATH/LANG, HOME at the role's fresh state root,
XDG_CACHE_HOME/XDG_CONFIG_HOME and CODEX_HOME beneath that root, binding path/SHA,
and AUTOSTOP_MANAGER_ENV_FILE. No caller environment is merged. All role env
files are `/etc/autostop-manager-cold/<epoch>/<role>/runtime.env`, root-owned,
role-readable and protected from group/world writes. All application consumers
use existing exact expected_paths; optional path environment overrides must
equal them, never an old namespace.

* manager-mcp: mandatory explicit AUTOSTOP_MANAGER_MCP_HOST=127.0.0.1,
  AUTOSTOP_MANAGER_MCP_PORT=41932 (initial external) or 41933 (final CRM), and
  AUTOSTOP_MANAGER_MCP_PATH=/mcp/__component/<epoch>. Component code rejects the
  default /mcp, another epoch or a noncanonical path. Inbound and outbound CRM
  routes are different endpoints. Configure explicit AUTOSTOP_CRM_MCP_URL,
  AUTOSTOP_CRM_MCP_BEARER_TOKEN and, when needed by its tools,
  AUTOSTOP_AUTOMATION_CRM_API_URL; the two URLs must equal admission.crm_urls.
* scheduler: explicit AUTOSTOP_AUTOMATION_CRM_API_URL, AUTOSTOP_CRM_MCP_URL and
  AUTOSTOP_CRM_MCP_BEARER_TOKEN for the reviewed feed; do not guess a Board nonce.
  Registry/control/work socket defaults come from the binding. A fresh registry
  starts under its existing hold and has no imported old runnable jobs/outbox.
* work-telegram: AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1,
  AUTOSTOP_WORK_TELEGRAM_DURABLE_INBOX=1 and
  AUTOSTOP_WORK_TELEGRAM_WAKE_SOCKET equal to the same epoch wake socket. Its
  credentials are role-owned 0600 (TelegramConfig requires the actual UID),
  not a root-owned group-readable secret; auth-only account.session is separately
  staged as that role. owner.json remains root-owned group-readable.
* personal-telegram: its own role-owned 0600 credentials and auth-only session;
  no borrowed work inbox/outbox/cursors. There are no additional required env
  keys for its fixed daemon entrypoint.
* j1: its binding defines the fresh cache and VIN scratch roots. Preserve only
  reviewed provider configuration such as AUTOSTOP_J1_SEARXNG_URL when required;
  no legacy cache or runnable job adoption.
* wake: wake.json is separately staged with the new owned kernel thread/socket,
  exact source/instruction root/revision and role-local HOME. No old CODEX_HOME
  or app socket adoption; no additional env key authorizes a turn.

Store URL/tokens, when tools are enabled, are explicitly staged from the
compatible preserved Store54 credentials. Their presence grants no mutation:
store_effects_ref remains null until separately approved. No scope/key repair
occurs during this stage.

CLOSED health uses read-only own-instance evidence: native PID/birth/cgroup and
runtime/config hashes; MCP GET /internal/store-workers; scheduler control status
and its held registry; bridge status/owner status plus actual frontier receipts;
wake socket status plus the new kernel/socket owner; J1 process/cache ownership.
These status requests do not publish OPEN or prove an unknown operation safe.
Any missing/held/quarantine-overflow, wrong account, stale process/instance,
nonempty imported runnable queue or unsupported legacy CRM capability is STOP.

For revocation, root creates/fsyncs the public epoch
`dispatch/revoked.json` marker FIRST under the same EX flock, before private
C/revoked.json or closing state. Any marker, even partial, denies permanently.
Do not place it under proofs; workers cannot read the private attempt root.

## Boundary and qualification

Manager-external has no `crm-embedded` claim. CRM-mode grants exactly its seven
new instances and links the initial six-role commit. Neither mode accepts a joint projection as a component commit or vice
versa. Binding configuration cannot publish OPEN, clear an old hold, restart an
old unit, issue credentials, transfer provider backlog or retry an unknown call.

At final CRM activation, external roles still have their old-CRM URL/env binding.
The CRM-stage plan explicitly rebinds/restarts those six with new launch
claims/configuration, the same source/runtime artifact, and cold URLs under the
seven-role CRM admission. Enabling deny-old-routes without that handover is
STOP; no dynamic URL renewal or third mode is implemented here.

All examples/tests use artificial identities and private I/O. They do not prove
current production retirement, a fresh Telegram frontier, actual principal
scopes or an installed/authorized runtime. No deployment is performed here.
