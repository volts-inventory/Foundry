---
name: repo-bootstrap
description: Record this repo's build/lint/test commands into CLAUDE.md, split into fast and slow. Use when CLAUDE.md has no "## Dev commands" section, or has one that is not split into fast and slow — the Verification rule depends on that split existing.
disable-model-invocation: true
---

# Repo bootstrap

Run this once per repo. It is finished when `## Dev commands` exists and is
split into fast and slow.

## Why the split matters

Verification says slow work needs the user's go-ahead. Without a fast/slow
split, "anything marked slow" resolves to an empty set and every session is
back to guessing — so an unsplit section is a bootstrap that hasn't
finished, not a bootstrap that was skipped.

## Procedure

1. Find the repo's real commands. Check README, CONTRIBUTING, Makefile,
   `package.json` scripts, `pyproject.toml`, CI config.
2. Record them verbatim under `## Dev commands` in CLAUDE.md.
3. Split into:
   - **Fast** — safe to run unprompted after a change. Syntax checks,
     typechecks, single-file or single-test runs, lint.
   - **Slow** — needs the user's go-ahead. Full suites, full builds, e2e,
     container builds, anything that provisions a stack.
4. Put a rough runtime next to each slow entry. If you can't measure it, say
   "runtime unmeasured" rather than guessing a number.
5. Include the single-test / single-file invocation under Fast. Verification
   requires running the one test covering a change, so that command has to
   be written down.

## Getting the invocation right

A fresh clone often has no system-level toolchain. A recorded command
failing with "not found" usually means it needs the repo's own runner
(`uv run …`, `npm ci` first, a virtualenv) rather than that the command is
wrong. Record the working invocation, prefix and all.
