# Shared Search Budget Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; implement inline with tests first.

**Goal:** Every work item shares its search ceiling across candidates; retain each valid unique hit immediately, stop at target, resume only missing results.

**Architecture:** Drive deterministic single-attempt searches from a work-item cursor. Resolve automatic counts for each attempt, independently validate before retaining, and preserve existing deduplication/ranking. Keep cumulative telemetry separate from the current configuration cursor so parameter edits can restart the new search space.

**Tech Stack:** Existing Python services and vanilla JavaScript frontend.

**Spec:** Current user request: 2,000 attempts total per work item, five requested candidates, stop as soon as five are found; four retained results survive continuation. No relaxed validation, external repository writes, or service restart.

## Global Constraints

- Keep existing candidates and manual selections on continuation.
- Budget is per template/plan work item, not per candidate and not shared across templates.
- Only completed attempts advance the cursor; cancellation replays at most the interrupted attempt.
- Old snapshots retain candidates; legacy spent attempts count toward the new shared ceiling.
- UI must describe shared limits and avoid slot-based time/probability extrapolation.

## Review Focus

- Cancellation during validation must not skip an uncommitted hit.
- Same-budget continuation must not replay completed attempts.
- Parameter changes reset only the changed work item's cursor.
- Repeated signatures and invalid candidates must not satisfy the requested count.
- Partial results and existing selections must survive progress, cancellation and continuation.

### Task 1: Shared search and continuation

Files: scripts/services/job_service.py; scripts/tests/test_job_service.py.

- [x] Add failing tests for early stop, shared exhaustion, partial retention and incremental continuation.
- [x] Add shared_attempts_completed cursor; run single-attempt windows with deterministic per-attempt parameter resolution. Preserve independent validation and deduplication.
- [x] Verify focused tests including cancellation and parameter conflicts.

### Task 2: API and UI budget semantics

Files: scripts/app.py; frontend/assets/app.js; frontend/index.html; corresponding tests and frontend/UX-CONTRACT.md.

- [x] Add budget-summary tests proving remaining work is not multiplied by missing candidate count.
- [x] Preserve cursor on unchanged parameters; reset on changed parameters. Report shared progress and remaining candidates.
- [x] Update existing budget panel labels/calculation without restyling.
- [ ] Run backend and frontend suites, real fixture benchmark, and independent review; commit and push only scoped changes.

## Execution ledger

- User already requested direct implementation without repeated confirmation. Execute in current feature branch, preserving unrelated working-tree changes.
- Single-attempt windows deliberately reuse the tested search engine rather than duplicate its proof/validation implementation. Benchmark orchestration overhead before shipping.
- Independent review caught and resolved attempt-index alternation and seed-ordinal provenance. Shared draw ordinal is distinct from visible candidate number; replay tests cover the recorded digest.
- Final backend verification: `PYTHONPATH=scripts:scripts/tests python3 -m unittest discover -s scripts/tests`: 529 tests, 17 environment-dependent skips, no failures. Frontend: `node --test frontend/tests/*.test.mjs`: 129 passed.
- Real read-only fixture from active job: lessTxkd031, five types, quantity 45–55%, target four candidates, isolated ceiling 50. Found all four in 20 attempts / 47.858 seconds; continuing an already-complete result did no work and preserved IDs. This is a small-budget smoke test, not a guarantee for all templates or a calibrated production difficulty benchmark.
- Browser plugin unavailable; isolated Playwright/installed Chrome fixture at 127.0.0.1:59998 verified 1440×1000 and 390×844, no page errors, retained 4/5 display, budget selection and keyboard focus. Selecting 4× after 2,000 attempts correctly adds 6,000, not multiplied by the missing candidate count. No user job or service was changed.
- Premium strict static audit: 73 findings on both HEAD baseline and current tree, no new rule/file/message finding. Impeccable detector also reports existing styles; no CSS was changed and no ignores were added. Existing unrelated design debt is outside this search-semantics change.
