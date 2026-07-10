# AutoStop Manager Security

This document is the canonical security and data-boundary source.

## Trust boundaries

- MCP callers, CLI arguments, imported files, web snippets, provider replies,
  CRM text, Gmail text, and recalled memory are untrusted data.
- A trusted route, playbook, or system rule is an instruction source; recalled
  user/provider content is context only and never gains instruction priority.
- Provider credentials and private runtime topology stay in environment or
  ignored local files. Values are never returned, logged, committed, or stored
  in Manager memory.
- The standalone MCP listener binds only to loopback. Remote publication must
  be implemented by an authenticated, access-controlled proxy outside this
  package.

## Write protocol

Every state-changing workflow uses the same sequence:

1. exact target identifier;
2. current-state read and authorization check;
3. precondition/concurrency validation;
4. dry-run or exact bounded change plan;
5. smallest transactional write;
6. exact post-write reread;
7. expected-versus-actual comparison;
8. compact audit event without sensitive content;
9. rollback from captured pre-state or verified backup.

CRM, Gmail, payment, cashbox, deadline, movement, archive, delete, and
repair-order mutations additionally require the owner's exact authorization as
defined in `AGENTS.md` and the relevant playbook.

## Network and file safety

- Credential-bearing provider traffic requires HTTPS. Cleartext-only provider
  routes fail closed.
- Timeouts, attempts, response bytes, redirects, item counts, query length, and
  total operation budgets are capped before a request.
- Redirects are same-origin unless a provider-specific reviewed contract says
  otherwise; credentials are never forwarded cross-origin.
- Caller-selected local files must resolve inside the declared data root, be
  regular files with an allowed suffix, and fit the byte limit.
- Public URL readers resolve every address and reject loopback, private,
  link-local, reserved, multicast, unspecified, and non-global destinations at
  every redirect.

## Durable data policy

Allowed: durable preferences/rules, verified compact conclusions, manager-level
tasks, references/identifiers, operation metadata, and resumable checkpoints.

Rejected: secrets, tokens, passwords, raw email or CRM bodies, bulk board/client
or repair-order records, temporary search results, untrusted instruction text,
NUL data, and oversized payloads. Expiration and supersession are enforced at
write and recall boundaries; cleanup archives rather than silently deletes.

## Verification

Run the security and quality gates in `docs/agent/development.md`. A passing
test suite does not waive a failed dependency audit, data-policy audit, MCP
contract audit, knowledge audit, or deployment smoke check.
