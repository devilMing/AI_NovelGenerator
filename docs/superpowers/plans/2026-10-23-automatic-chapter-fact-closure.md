# Automatic Chapter Fact Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make volume one-click writing automatically extract, independently review, persist, and reuse chapter-after facts before the next chapter starts.

**Architecture:** Add an append-only, revision-bound `chapter_fact_batches` ledger. The volume-writing state machine checkpoints extraction, review, repair, and transactional adoption in its existing pending operation; `writing_context` resolves accepted ledger facts visible before the target chapter and fingerprints them as generation input. Existing entity fact tables remain untouched in this first safe phase, while the ledger becomes the canonical automatic chapter-fact source for subsequent AI writing.

**Tech Stack:** FastAPI/Pydantic, Motor/MongoDB transactions, existing `structured_advice` model adapter, React/TypeScript volume-writing timeline, Python `unittest`, Node test runner.

---

### Task 1: Define the revision-bound fact contract

**Files:**
- Create: `backend/services/novel/chapter_fact_contract.py`
- Test: `scripts/test_chapter_fact_closure.py`

- [ ] **Step 1: Write failing contract tests**

Add tests proving that a batch rejects duplicate `(subject_type, subject_id, field)` keys, empty evidence, invalid indices, and no-op state transitions.

- [ ] **Step 2: Run the contract tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest scripts.test_chapter_fact_closure.ChapterFactContractTests -v`

Expected: import failure because `chapter_fact_contract` does not exist.

- [ ] **Step 3: Implement strict extraction and review models**

Define `ChapterFactProposalV1`, `ChapterFactExtractionV1`, `ChapterFactDecisionV1`, and `ChapterFactReviewV1`. A proposal contains an existing subject, a stable field, optional `before`, required `after`, exact `{start, quote}` evidence, and normal/high risk. A review contains exactly one accept/omit/block decision per proposal.

- [ ] **Step 4: Run the contract tests and verify GREEN**

Run the Step 2 command; expected all contract tests pass.

### Task 2: Extract, review, repair, and atomically adopt facts

**Files:**
- Create: `backend/services/novel/chapter_fact_service.py`
- Modify: `backend/services/novel/volume_writing_service.py`
- Test: `scripts/test_chapter_fact_closure.py`

- [ ] **Step 1: Write failing service tests**

Cover exact-quote validation, subject allow-list validation, unsupported fact omission, blocking review repair (maximum two attempts), high-risk double review, idempotent adoption, and revision/hash mismatch rejection.

- [ ] **Step 2: Run the service tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest scripts.test_chapter_fact_closure.ChapterFactServiceTests -v`

Expected: missing extraction/adoption functions.

- [ ] **Step 3: Implement the service**

Use two independent `structured_advice` calls: one produces proposals, another decides accept/omit/block. Re-read the immutable revision before every model request. Validate every quote with Python code-point slicing. Retry blocking results with a repair request at most twice. Persist only accepted facts in one `chapter_fact_batches` transaction with the revision id/hash, all attempts, final review, model usage, and a deterministic batch id.

- [ ] **Step 4: Integrate the volume state machine**

After body adoption, retain `pending.saved_chapter` and run stages `fact_extracting`, `fact_reviewing`, and `fact_saving`. Checkpoint every paid result. Resume from the first missing checkpoint and never regenerate or overwrite the saved body. Clear `pending` only after a committed fact batch or an explicit no-change batch exists.

- [ ] **Step 5: Run the service tests and verify GREEN**

Run the Step 2 command; expected all service tests pass.

### Task 3: Feed confirmed chapter facts into the next chapter

**Files:**
- Modify: `backend/services/novel/writing_context.py`
- Test: `scripts/test_chapter_fact_closure.py`
- Test: `scripts/test_volume_continuation.py`

- [ ] **Step 1: Write failing context tests**

Prove that chapter two receives accepted facts from chapter one, omitted facts never appear, future-chapter facts are invisible, and changing a fact batch invalidates a stale writing candidate.

- [ ] **Step 2: Run the context tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest scripts.test_chapter_fact_closure.ChapterFactContextTests -v`

Expected: `confirmed_chapter_facts` is absent.

- [ ] **Step 3: Implement positional fact resolution**

Resolve batches only from chapters visible at the target `chapter_before` position, fold state facts by stable key in story order, retain event-like facts, expose them as `confirmed_chapter_facts`, and include the exact result in `context_basis.reads`.

- [ ] **Step 4: Run context and continuation tests**

Run: `.venv\Scripts\python.exe -m unittest scripts.test_chapter_fact_closure scripts.test_volume_continuation -v`

Expected: all tests pass and the second body is generated only after the first fact batch commits.

### Task 4: Display the automatic fact closure in logs

**Files:**
- Modify: `frontend/src/types/volumeWriting.ts`
- Modify: `frontend/src/i18n/messages/zh.json`
- Modify: `frontend/src/i18n/messages/en.json`
- Modify: `frontend/src/lib/writingLogLabels.ts`
- Test: `frontend/src/lib/writingLogLabels.test.ts`

- [ ] **Step 1: Write failing label tests**

Assert localized labels for extraction, independent review, repair, adoption, and no-change completion.

- [ ] **Step 2: Run the frontend test and verify RED**

Run: `node --experimental-strip-types --test src/lib/writingLogLabels.test.ts`

Expected: new fact-stage labels are missing.

- [ ] **Step 3: Add stage types and labels**

Expose `fact_extracting`, `fact_reviewing`, and `fact_saving` stages. Map backend log instance paths to readable labels and retain the generic detail viewer for proposals, evidence, decisions, accepted/omitted counts, repair attempts, and batch id.

- [ ] **Step 4: Run the frontend test and verify GREEN**

Run the Step 2 command; expected all label tests pass.

### Task 5: Regression, documentation, and runtime handoff

**Files:**
- Modify: `docs/first-chapter-writing.md`

- [ ] **Step 1: Document the automatic boundary**

Document that accepted automatic facts are append-only, revision-bound, and consumed by later writing; unsupported/omitted proposals are logged but not facts; unresolved blocking review pauses the job; editing a source chapter requires a later reconciliation phase and therefore blocks claiming full-book unattended guarantees.

- [ ] **Step 2: Run the full backend regression suite**

Run the existing first-chapter, volume-writing, streaming, and new fact-closure tests. Expected: zero failures.

- [ ] **Step 3: Run frontend tests, TypeScript, ESLint, and production build**

Expected: zero test failures, zero type errors, zero lint errors in touched files, and successful Next production build.

- [ ] **Step 4: Restart the project and perform a read-only UI check**

Restart only the project-scoped backend/frontend processes. Open the existing novel and verify the volume panel displays the new fact stages without starting a real generation run or modifying existing prose.

