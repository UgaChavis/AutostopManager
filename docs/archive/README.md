# Documentation Archive

This folder is only a short parking area for historical implementation notes
that still need review. Fully migrated or obsolete plans should be deleted, not
kept indefinitely.

Active agent behavior, command routing, MCP catalogs, source routes, and
knowledge-base navigation live under `docs/agent/`.

Rules for archived files:

- do not treat archived plans as current instructions;
- do not add archived files to `knowledge_map.json` primary routes;
- if an archived idea becomes active again, move the current rule into the
  smallest relevant playbook under `docs/agent/`;
- after the active rule exists in `docs/agent/`, remove the old plan in the
  next documentation hygiene pass.
