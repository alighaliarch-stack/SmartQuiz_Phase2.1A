# SmartQuiz — Phase 2 v2 Freeze Report

**Package name:** `SmartQuiz_Phase2_Classroom_Radio_v2_Frozen.zip`
**Freeze date:** 6 September 2026
**Version label:** Phase 2 v2 (Classroom Radio baseline + two post-review fixes)
**Status:** Reviewed and accepted. This document freezes the exact state described below — no new features, no refactoring, no logic changes beyond what's recorded here.

This is a checkpoint on top of `SmartQuiz_Phase2_ClassroomRadio.zip` (the original Phase 2 Classroom Radio delivery). It adds exactly two fixes, described in full below. Everything from Phase 1 (`PHASE_1_CHANGELOG.md`, `PHASE_1_README.md`) and the original Phase 2 Classroom Radio feature (`PHASE_2_CLASSROOM_RADIO.md`) is preserved unchanged and still applies — this report covers only what changed since then.

---

## Issue 1 — Independent per-student Auto Advance

**The problem.** When Auto Advance was on, a student who submitted early still had to wait for the shared question timer to run out before the class moved on — there was no path for an individual student's own progression.

**The clarified requirement.** Auto Advance ON means each student paces themselves independently:
- Student 1 submits at 20s of a 60s question → Student 1 moves to the next question immediately.
- Student 2 is still thinking → Student 2 stays on the current question, unaffected by Student 1.
- Student 2 can keep going until they submit or their own timer expires.
- The shared/global session question index must never be advanced by an individual student's early submission.
- Auto Advance OFF is the original, fully synchronized, teacher-controlled flow — completely unchanged.

**What was implemented.**
- `Student.current_question_index` and `Student.question_started_at` (new columns) — a student's own personal position and timer anchor, used *only* when the quiz's `auto_advance` is `True`.
- `_student_effective_question_index()`: returns the global session index unchanged when Auto Advance is off. When it's on, returns `max(student's own progress, global index)` — this means the teacher's existing Next Question / Complete Quiz controls needed **zero code changes**: in Auto-Advance-ON mode they now act as a class-wide "catch everyone up" override, and a student who's already ahead is never pulled backward.
- `_student_effective_question_started_at()` / `_student_question_ends_at()`: the matching per-student timer math, mirroring the existing global timer functions exactly.
- `_ensure_student_question_state()`: lazily starts a student's personal pacing the first time they need it (whether they were waiting when Start Quiz was clicked, or joined afterward).
- `session_question_submit` advances a student's own pointer the instant they submit (when Auto Advance is on), guarded so a late/stale submission can't advance them twice or out of order. The submission response now includes `next_question_number`, so the client can redirect immediately rather than waiting for the next poll cycle — this is what makes the transition genuinely instant, not just "faster."
- `/session/{code}/status` gained an additive `student_current_question` field (and `student_question_ends_at`) used for navigation. Every pre-existing field (`current_question`, `current_question_index`, etc.) is completely unchanged in meaning and value.
- All three student-facing polling templates (`session_question.html`, `waiting_room.html`, `session_waiting_next.html`) navigate on `student_current_question` instead of the raw global field.

## Issue 2 — Quiz reuse after a session ends

**The problem.** `finish_quiz` and `end_session` set `quiz.status = "Completed"`, and `start_session` requires `quiz.status == "Published"` — so a quiz became permanently unstartable after its first session ended.

**A second, related risk found during inspection.** `Response` rows only stored a `question_id` foreign key, not a copy of the question's content. Both results-rendering functions (`_compute_results`'s Needs-Review builder and `_student_own_results`) read the question's text/options/correct-answer *live* from the current `Question` row — meaning editing a question to prepare a new session would have silently rewritten what an already-completed session's results displayed.

**What was implemented.**
- `finish_quiz` / `end_session` now revert the **quiz** to `"Published"` (not `"Completed"`) once its session ends — the quiz becomes immediately reusable, while the **session's own** record (status, `ended_at`, every response tied to that `session_id`) is completely untouched.
- The dashboard's "Completed Quizzes" stat, which depended on the status value that no longer occurs, now counts distinct quizzes with at least one historically-completed session instead — a more accurate metric that survives quiz reuse correctly.

### Historical question snapshots

- Five new nullable columns on `Response`: `question_text_snapshot`, `question_type_snapshot`, `question_options_snapshot`, `correct_answer_snapshot`, `question_points_snapshot`.
- Populated once, at submission time, from the question's actual content at that exact moment.
- Both results-rendering functions now prefer the snapshot over the live `Question` row. Verified directly: editing a question's text and correct answer *after* a session ended does not change what that session's results display, for both a deterministically-graded response (student's own results page) and an AI-graded Short Answer pending review (teacher's Needs-Review view).
- **Known, documented limitation:** a response created before this fix existed has no snapshot to fall back on and still reads the live `Question` row (there was never a captured snapshot for it — unavoidable for pre-existing data). Similarly, a question a student never answered has no snapshot (nothing was ever captured for it), so an "unanswered" row still reflects the live question if it's later edited.

### Session History

- The quiz builder page now lists every past session for that quiz (join code, started/ended timestamps, status, student count, a link to that session's own Results page). This didn't exist before and was necessary to make "Session 1 records remain permanently available" actually *discoverable* by a teacher, rather than only reachable by manually constructing a URL with a session ID they'd have to already know.

---

## Database migrations

| Table | Column(s) added | Default for existing rows |
|---|---|---|
| `students` | `current_question_index` | `0` (meaning "not personally started — defer to the global session state," which is also the correct behavior for every Auto-Advance-off quiz) |
| `students` | `question_started_at` | `NULL` |
| `responses` | `question_text_snapshot`, `question_type_snapshot`, `question_options_snapshot`, `correct_answer_snapshot`, `question_points_snapshot` | `NULL` (pre-existing responses fall back to the live Question object, as documented above) |

Both migrations are automatic on startup, non-destructive, and were tested against the running application without any manual intervention required.

---

## Tests performed (this freeze cycle)

All of the following were actually executed against the running application — API-level tests via `requests`, plus two full real-browser tests via Playwright, not code review alone.

**Issue 1:**
- Auto Advance OFF: early submission does not advance the student; `next_question_number` absent; `student_current_question` exactly equals the global index — confirmed byte-identical to pre-fix behavior.
- Auto Advance ON: a student submitting early is told to advance immediately (`next_question_number` in the same response), while a second student polling at the same moment is confirmed still on the original question; the global session index is confirmed unchanged at the database level.
- Full sequential auto-advance through all three non-Short-Answer question types (MCQ single, MCQ multiple, True/False), correctly reaching `student_completed=True` only on the final question.
- Duplicate submission protection re-confirmed intact under Auto Advance (no double-advance).
- **Real browser test**: two separate browser pages (Alice, Bob) — Alice's browser navigated to the next question immediately upon her clicking Submit, with no wait for the 60-second timer; Bob's browser independently remained on the original question; both eventually converged after each submitted on their own schedule. Zero JavaScript console errors.

**Issue 2:**
- Quiz status confirmed reverting to `Published` (not stuck on `Completed`) after `finish_quiz`, while the session's own row correctly stayed `Completed`.
- Full two-session reuse workflow: Session 1 (2 students) → ended → Session 2 started on the same quiz → confirmed a genuinely different join code, confirmed Session 2 begins with zero students/responses inherited from Session 1, confirmed both sessions' leaderboards are completely independent, confirmed student privacy (token-based access) still correctly blocks cross-student access after reuse, confirmed Session 2's Classroom Radio state starts completely fresh (`STOPPED`, position 0, no track, revision 0) rather than inheriting anything from Session 1.
- Snapshot protection confirmed on both code paths: a student's own results page (deterministic MCQ grading) and the teacher's Needs-Review view (AI-graded Short Answer) — in both cases, editing the question after the session ended did not change what that session's results displayed.
- **Real browser test**: full teacher workflow — finish Session 1 via real button clicks, navigate to the quiz builder, confirm the Session History section lists Session 1 with a working results link, confirm a "Start Session" button is available again, click through to start Session 2, confirm a new join code. Zero JavaScript console errors.

## Phase 1 regression tests (this freeze cycle)

Authentication (unauthenticated redirect, wrong password rejected, correct password succeeds), student privacy (own results accessible, wrong token rejected), all four question types graded correctly including MCQ-multiple's proportional scoring, concurrent duplicate submission protection (10 simultaneous requests → exactly one stored response), and leaderboard sorting (highest score first) — all re-confirmed passing on top of both fixes.

## Phase 2 regression tests (this freeze cycle)

Music library upload validation (valid WAV accepted, invalid file type rejected), track selection, play/pause/resume (resume continues from the paused position, not zero), stop (→ 0), reset while playing (→ 0, stays playing), seek ±10 seconds, radio independence from quiz question progression (confirmed the radio's revision number is completely unaffected by advancing a question), late-join synchronization (a student checking status after 4 seconds of playback computes ~4 seconds, not zero), and reconnect synchronization (a student checking status after the teacher paused correctly sees the frozen paused position) — all re-confirmed passing.

## Known limitations

- A `Response` row created before this fix has no historical snapshot and falls back to the live `Question` row — unavoidable for pre-existing data, not a defect.
- An unanswered question in a student's own results breakdown has no snapshot either (nothing was ever captured for a question the student never touched), so it still reflects the live question if later edited — a narrow, explicitly scoped and documented gap, not a silent one.
- No load/stress testing beyond 10 concurrent threads was performed for the duplicate-submission protection.

## Exact files changed since the original Phase 2 Classroom Radio delivery

`models.py`, `database.py`, `app.py`, `templates/session_question.html`, `templates/waiting_room.html`, `templates/session_waiting_next.html`, `templates/quiz_builder.html`. No files were added or removed.

## Package identity

- **Filename:** `SmartQuiz_Phase2_Classroom_Radio_v2_Frozen.zip`
- **Freeze date:** 6 September 2026
- **Version label:** Phase 2 v2 (Classroom Radio + Issue 1 per-student Auto Advance + Issue 2 quiz reuse/historical integrity)
