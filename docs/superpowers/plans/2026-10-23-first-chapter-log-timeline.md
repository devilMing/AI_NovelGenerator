# First-chapter Log Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat first-chapter log with a read-only business-step timeline, while preserving node output, failures and superseded work.

**Architecture:** Add a scoped timeline endpoint projecting existing step/checkpoint/log records without business writes. Reuse the existing node log viewer with a step filter. Show saved steps separately from superseded attempts, and treat saved checkpoints as authoritative even if a historical attempt failed.

**Tech Stack:** FastAPI, async PyMongo, Next.js, React, node:test, unittest against isolated Mongo databases.

### Task 1: Read-only timeline and filtered node logs

Files: modify `backend/services/novel/first_chapter_service.py`, `backend/api/default_routers/first_chapter_router.py`, `scripts/test_first_chapter_runtime.py`; create `backend/services/novel/first_chapter_log.py`.

- [x] Add failing runtime tests: `page = await service.timeline(novel, job_id)` must return one saved operation despite earlier failed log records; a superseded operation must appear only in history; `service.logs(..., step_id=id)` must exclude other operations and redact credentials. Cross-novel/job step IDs must fail with 404.
- [x] Run `.venv\Scripts\python.exe -m unittest scripts.test_first_chapter_runtime -q` and confirm new tests fail because the new API is absent.
- [x] Implement summary projection using step IDs, saved/superseded flags, review summary and projected log metadata. Use original stored timestamps; wall duration includes pauses and retries and is explicitly labelled. Add `GET /{job_id}/timeline` and optional `step_id` to existing logs route. Never return candidate/basis/request secrets.
- [x] Run runtime tests again; all must pass.

### Task 2: Timeline UI and completed-run panel

Files: create `frontend/src/components/writing/novel-info/FirstChapterTimeline.tsx`, `frontend/src/lib/firstChapterTimeline.ts`, `frontend/src/lib/firstChapterTimeline.test.ts`; modify `FirstChapterWriting.tsx`, `VolumeWritingLog.tsx`.

- [x] Add failing tests for summary state rules and completed-run form visibility: `canStartFirstChapter({active:false,status:'completed'}) === false`; node state running for a saved step or inactive run must become stopped without hiding historical failures.
- [x] Run `node --experimental-strip-types --test src/lib/firstChapterTimeline.test.ts` and confirm expected failures.
- [x] Render numbered steps with save status, result, start/save time, wall duration and failed-record count. Expand step details on demand, requesting only that operation's nodes. Keep superseded work in a separate collapsed history. Poll only active runs and abort reads on close/switch.
- [x] Apply existing volume-modal padding/header/footer conventions to the first-chapter modal. Label selected run timestamps as start time; hide guidance, checkbox and start action when the current run is completed.
- [x] Run frontend node tests, TypeScript and scoped ESLint; then inspect real completed run and older cancelled run via the browser without starting generation.

### Task 3: Integration verification

- [x] Run the first-chapter backend and response-stream regression suite, `git diff --check`, and production frontend build.
- [x] Restart only verified project backend processes after confirming no active writing jobs; preserve per-project environment.
- [x] Verify live timeline shows 16 saved steps plus superseded history, node details retain failures, no completed-run start form, and chapter hash/revisions remain unchanged.
- [x] Document the new read-only timeline and validation results. Do not commit or push without a user request.
