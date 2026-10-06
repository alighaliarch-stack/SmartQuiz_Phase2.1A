# SmartQuiz — Phase 2.1A Freeze Report

**Package name:** `SmartQuiz_Phase2.1A_Student_Identity_Frozen.zip`
**Scope:** Student Identity, Duplicate-Join Prevention & Reconnection
**Status:** Reviewed and accepted. This document freezes the exact state described below — no new features, no refactoring, no logic changes beyond what's recorded here.

This is a checkpoint on top of `SmartQuiz_Phase2_Classroom_Radio_v2_Frozen.zip`. Everything from Phase 1 (`PHASE_1_CHANGELOG.md`, `PHASE_1_README.md`), the original Classroom Radio feature (`PHASE_2_CLASSROOM_RADIO.md`), and the Phase 2 v2 fixes (`PHASE_2_V2_FREEZE_REPORT.md` — per-student Auto Advance, quiz reuse, historical question snapshots) are preserved unchanged and still apply — this report covers only what changed since then.

---

## 1. The problem

`POST /join` created a brand-new `Student` row on every submission, with no identity check of any kind. A resubmit, a page refresh that landed back on the join form, or a reconnect after a dropped connection all produced a new, orphaned attempt for the same physical student — losing their `result_token`, their Auto Advance progress, and fragmenting their responses/leaderboard entry across multiple rows.

## 2. Exact files changed

Three files, all pre-existing — no files added or removed, no new dependencies:

| File | Change |
|---|---|
| `models.py` | Added `Student.normalized_roll_number` column + docstring updates. |
| `database.py` | Added `normalize_roll_number()`, `_student_duplicate_identity_groups()`, `_migrate_student_session_roll_uniqueness()`, and the migration steps that call them. |
| `app.py` | Added `_find_existing_attempt()`; rewrote `join_submit` to look up and reconnect to an existing attempt before creating a new one, with `IntegrityError` handling for the concurrent case. One import line added (`normalize_roll_number` from `database`). |

No templates, no other routes, and no scoring/timer/radio/auth code were touched.

## 3. Database schema / migration change

- New column: `students.normalized_roll_number VARCHAR(50)` (nullable) — comparison-only identity; the original `roll_number` column is untouched and still holds the student's verbatim input for display.
- New index: `uq_students_session_normalized_roll` — a **partial UNIQUE index** on `(session_id, normalized_roll_number) WHERE normalized_roll_number != ''`. A plain unique index (not a table-level constraint) was used deliberately, since SQLite can only add a table-level UNIQUE constraint by rebuilding the whole table, and `students` is referenced by `responses` via foreign key — a unique index gives the identical guarantee without that risk. The partial clause, together with SQLite's own NULL-is-distinct behavior, means a blank/whitespace-only roll number or a NULL `session_id` is never treated as colliding with another such row.
- Migration behavior (in `_run_migrations()`, same idempotent style as every other migration in the file): add the column if missing → backfill any row still missing a normalized value (a no-op once populated, re-checked harmlessly on every startup) → check if the unique index already exists → if not, scan for pre-existing `(session_id, normalized_roll_number)` duplicates. If any exist, the index is **not** created this run, a diagnostic is printed naming each affected pair and its row count, and every row is left untouched. If none exist, the index is created. This re-runs on every startup, so it self-heals the moment old duplicates are resolved.

## 4. Identity rule

`(Session.id, normalize_roll_number(roll_number))` identifies one student attempt. `normalize_roll_number()` strips leading/trailing whitespace, collapses internal whitespace to a single space, and lowercases — nothing else is altered (hyphens, slashes, digits, leading zeros are preserved exactly). Name, course, and section are never part of the identity — two different students may share a name, and a shared name never merges two different roll numbers.

## 5. Duplicate-join behavior

`join_submit` looks up an existing `Student` by `(session_id, normalized_roll)` before creating one. If found, it redirects straight to that student's existing waiting-room/session URL **without modifying the row at all** — name, course, section, `current_question_index`, `question_started_at`, `result_token`, and every existing `Response` are left exactly as they were. Only if no existing attempt is found is a new row created.

## 6. Reconnect behavior

No template or polling changes were needed. The existing waiting-room/question-page routes already compute a student's own effective question and pre-fill any existing response from their `Student` row, so reconnecting to a preserved row lands the student back exactly where they left off — verified with a real student mid-Auto-Advance (answered Q1, "left" via a new browser context, rejoined with the same roll number, landed directly back on Q2).

## 7. Concurrency protection

Two near-simultaneous joins for the same `(session_id, normalized_roll)` can both pass the pre-insert lookup before either commits — the actual backstop is the database's unique index. On `IntegrityError`, `join_submit` rolls back, re-queries the row that won the race by the same key, and redirects to it, mirroring the existing pattern already used for duplicate-answer submissions in `session_question_submit`. Verified with 10 concurrent threaded HTTP requests and, separately, two simultaneous real-browser joins — both produced exactly one database row, with every request (winner and losers alike) redirected to that same row.

## 8. Result-token preservation

Untouched by design: since a reconnect reuses the existing row as-is, its original `result_token` remains the token for that attempt. Verified directly — the token before and after a reconnect (including one resolved via the concurrency path) was confirmed byte-identical, and the existing privacy checks (correct token → 200, wrong token → 404, another student's token → 404) were re-confirmed unaffected.

## 9. Auto Advance compatibility

No changes to any Auto Advance function. Because reconnect never writes to `current_question_index`/`question_started_at`, a student's per-student progress and timer anchor survive a reconnect exactly as they were. Verified for both Auto-Advance-on (per-student pacing preserved across reconnect) and Auto-Advance-off (teacher-controlled global progression, unaffected by the identity change).

## 10. Historical-session behavior

The identity key is scoped to `session_id`, so the same roll number in a new session on the same (reused) quiz always creates a new, independent `Student` row. Verified directly: a student's Session 1 row, responses, and token were confirmed unchanged after that same roll number was used again in a newly started Session 2.

## 11. Old duplicate-record handling

Pre-existing duplicate rows (from before this fix) are never deleted or merged automatically. They are detected on every startup; if found, the unique index is withheld and a diagnostic naming the affected `(session_id, roll)` pairs and row counts is printed, while every row is left untouched. New joins are protected at the application level regardless. Verified end-to-end: a duplicate was manually injected into a real database, the exact warning and untouched-data behavior were confirmed, and the index was confirmed to appear automatically on the next startup once the duplicate was manually removed.

## 12. Tests performed

All executed against a real running instance (HTTP scripts, direct SQLite inspection, and Playwright/Chromium browser automation) — not inferred from code review:

- First join, repeat join, whitespace/case-normalized repeat join, a different student, and a same-name-different-roll student — each produced the correct row count.
- Concurrent duplicate join (10 threaded HTTP requests, and separately two simultaneous real-browser joins) — exactly one row each time.
- Responses, Auto Advance state (`current_question_index`, `question_started_at`), and `result_token` compared byte-for-byte before/after a reconnect — all preserved.
- Single leaderboard entry per attempt, confirmed via `/admin/results/data`.
- Session-reuse isolation: Session 1's data confirmed unchanged after the same roll number was used in a newly started Session 2.
- Phase 1 regression: `mcq_multiple` proportional/zero-on-wrong scoring, teacher-controlled global progression (Auto Advance off), and student-result token privacy (right/wrong/cross-student token) — all re-confirmed unaffected.
- Phase 2 regression: Classroom Radio status endpoint and `radio_revision` confirmed unaffected by quiz progression.
- Pre-existing-duplicate migration safety, reproduced directly against a hand-crafted "dirty" database (see §11), including confirming self-healing after manual resolution.
- **Real browser test** (Playwright/Chromium, avoiding `networkidle` given the app's continuous polling): a student joined, answered Q1, auto-advanced to Q2, "left" (new browser context), rejoined with the same name/roll, and landed directly back on Q2 with no second database row created; a different roll number joined independently; a plain page refresh preserved state; two simultaneous browser-driven joins with the same new roll number produced exactly one row. Zero server-side errors/exceptions observed in the application log across the entire run.

## 13. Known limitations

- No permanent automated test suite is committed to the project (true before this change as well, per the baseline audit); the tests above were run via ad hoc scripts against a live instance for this phase, not left behind as a checked-in suite.
- The partial unique index intentionally exempts an all-whitespace/blank roll number from the uniqueness check — a student who managed to submit a blank roll number is not deduplicated against another such submission. This matches the instruction not to introduce new validation scope beyond the identity fix itself.
- Pre-existing duplicate data (if any exists in a database that was already running before this fix) is left in place, by design, until a teacher/admin resolves it manually — no automatic reconciliation is attempted.

## 14. Exact excluded files / secrets

The package excludes, and contains **none** of the following:

- `smartquiz.db` (or any `.db` file) — no database is shipped; it is created automatically on first run.
- `.env` — only `.env.example` (placeholders/comments, no real values) is included.
- Any API key, password, password hash, or session secret — none exist as static config; the teacher password hash lives only in a runtime-created `smartquiz.db`, and the session secret is either generated at process start or read from an `.env` the operator supplies themselves.
- `__pycache__/` directories and `.pyc` files.
- Any virtual environment (`venv/`, `.venv/`).
- Any browser cookie/session-state file — none were ever part of this project; browser automation used for testing ran against a disposable local instance and wrote nothing into the project tree.
- Any test/scratch file — all verification scripts for this phase were written and run outside the project tree and are not part of the package.

## 15. Package identity

- **Filename:** `SmartQuiz_Phase2.1A_Student_Identity_Frozen.zip`
- **Contents:** the complete working SmartQuiz project — Phase 1 baseline, Phase 2 Classroom Radio, Phase 2 v2 (per-student Auto Advance, quiz reuse, historical question snapshots), and this Phase 2.1A student-identity/reconnection fix — as a clean, runnable source package with no runtime-generated data.
