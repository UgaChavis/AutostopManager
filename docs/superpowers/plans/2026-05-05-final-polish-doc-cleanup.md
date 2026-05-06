# Final Polish And Documentation Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce repository/documentation clutter, catch obvious file/documentation bugs, and leave the current AutoStopManager workspace in a verified final-polish state.

**Architecture:** Treat docs and knowledge routing as first-class app behavior. Remove only generated or clearly disposable artifacts; preserve source packs, private-runtime files, and uncommitted feature work unless proven obsolete.

**Tech Stack:** Python 3.12, pytest, AutostopManager CLI knowledge audits, PowerShell inventory scripts, Git status/diff checks.

---

### Task 1: Inventory Current State

**Files:**
- Inspect: repo status, tracked/untracked files, largest files, ignored generated outputs.

- [ ] Run `git status --short` and record dirty areas.
- [ ] Run file-size inventory for tracked and untracked files.
- [ ] Identify generated caches, temporary files, and heavy docs.

### Task 2: Documentation And Knowledge Audit

**Files:**
- Inspect: `docs/agent/**`, `docs/superpowers/plans/**`, `.gitignore`, generated data folders.

- [ ] Run `knowledge-audit`, `annotations-audit`, and `skills-audit`.
- [ ] Search docs for unfinished placeholder markers and stale generated references.
- [ ] Check knowledge routing after any doc cleanup.

### Task 3: Safe Cleanup

**Files:**
- Modify only if evidence is clear: `.gitignore`, generated temp dirs/files, docs indexes.

- [ ] Delete disposable generated caches/artifacts only.
- [ ] Add ignore rules for recurring generated outputs if needed.
- [ ] Avoid deleting source packs, private knowledge inventory, or owner-provided documents.

### Task 4: Bug Fixes And Verification

**Files:**
- Modify: code/tests only for failures found during audits or tests.

- [ ] Run focused checks after each fix.
- [ ] Run full `pytest`.
- [ ] Re-run knowledge/annotation/skill audits.

### Task 5: Final Report

- [ ] Summarize deleted files, changed files, verification results, and any remaining manual decisions.
