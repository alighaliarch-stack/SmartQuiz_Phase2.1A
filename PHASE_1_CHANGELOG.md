# SmartQuiz — Phase 1 Changelog

This document records what changed in Phase 1, on top of the working MVP that preceded it (multi-question live quizzes, timers, deterministic + AI-assisted grading, results/leaderboard, the orange/red/king-green visual identity). Every item below is labeled `[IMPLEMENTED]`, `[TESTED]`, or `[KNOWN LIMITATION]` — nothing is claimed as tested unless it was actually run against the application.

---

## 1. Individual duration per question

**[IMPLEMENTED]** `Question.duration_seconds` (new column, 15–1200s valid range). The shared, server-authoritative countdown timer now reads the *current question's own* duration instead of the quiz-wide `Quiz.duration_minutes`. `Quiz.duration_minutes` is preserved (not removed) — it still appears as a summary figure in the dashboard/builder, but is no longer consulted for timing anywhere.

**[IMPLEMENTED]** Validation rejects (not silently clamps) anything below 15s, above 1200s, or non-numeric, redirecting back to the question form with a clear message. Invalid values are never persisted.

**[IMPLEMENTED]** Migration backfills every pre-existing question's `duration_seconds` from *its own quiz's* prior `duration_minutes × 60` (clamped to the valid range) — not a generic default — so an existing quiz's timing behavior doesn't silently change the moment the migration runs.

**[TESTED]** Two questions in the same quiz measured independently via the live `/session/{code}/status` endpoint: ~19.7s and ~299.9s remaining respectively, confirming genuinely independent per-question timers. Boundary validation tested directly: 15s and 1200s accepted; 14s, 1201s, and a non-numeric value all rejected with no question persisted. Migration tested against a simulated pre-Phase-1 database with a real prior quiz (`duration_minutes=8`) — confirmed the question was backfilled to exactly `480` seconds, not a generic default, and the prior response row was preserved untouched.

---

## 2. Multiple-answer MCQ scoring

**[IMPLEMENTED]** `_score_mcq_multiple()` implements the specified policy exactly: `awarded = (correct options selected / total correct options) × question.points`, **unless** any incorrect option was also selected, in which case the whole response scores 0. Selecting nothing scores 0. `Response.score` changed from `Integer` to `Float` to hold this proportionally (e.g. 66.67), without lossy rounding compounding across multiple questions when summed for a leaderboard total.

**[IMPLEMENTED]** `Response.is_correct` for `mcq_multiple` means *full marks specifically* (`score == points`), not "any credit awarded" — a partial-credit response is not counted as "fully correct" for the leaderboard's correctness tie-breaker, matching the spec's own "number of fully correct answers" language.

**[TESTED]** Unit-tested directly against every example given in the spec (both the 2-correct/100pt and 3-correct/100pt cases — 13 scenarios total, including every "any wrong selected → 0" case and the 66.67/33.33 rounding) — all passed exactly. Verified end-to-end through a real browser: one student selecting 2 of 3 correct options (no wrong) scored 60.0/90; a second student selecting all 3 correct scored 90.0/90 with `is_correct=True`.

---

## 3. Leaderboard sorting

**[IMPLEMENTED]** Sort priority: total score (desc) → number of fully correct answers (desc) → average response time (asc) → student_id (asc, final deterministic tie-break — never arbitrary ordering). Fully session-scoped (a student attempting the same quiz across different live sessions is never aggregated across sessions).

Note: this exact ranking logic was already present from an earlier development phase, prior to Phase 1 — Phase 1's work here was verifying it met the spec (it did) and confirming it correctly incorporates `mcq_multiple`'s new proportional scores (it does, automatically, since it sums the raw `Response.score` field regardless of question type).

**[TESTED]** Verified with a real two-student scenario: a student who answered correctly ranked above one who answered incorrectly. Re-verified during the browser lifecycle test that a 90-point total correctly outranked a 70-point total.

---

## 4. Teacher authentication

**[IMPLEMENTED]** New `auth.py` module. Password hashing via PBKDF2-HMAC-SHA256 (stdlib `hashlib`/`hmac`/`secrets` only — no new third-party dependency for this). The hash is stored in a new generic `AppSetting` key-value table (not an environment variable), so it can be changed at runtime from Settings without editing `.env` or restarting the server.

**[IMPLEMENTED]** Every `/admin/*` route is protected by a single HTTP middleware gate (`_admin_auth_gate` in `app.py`), not by per-route dependencies and not by hiding UI elements — a route added later under `/admin` is automatically protected with no risk of forgetting to wire it up individually.

**[IMPLEMENTED]** Session identity uses Starlette's `SessionMiddleware` (signed cookie via `itsdangerous`, an existing transitive dependency of FastAPI — no new package). `SESSION_SECRET_KEY` can be set in `.env` for stable sessions across restarts; if unset, a random key is generated per process start (safe default — just means a restart signs everyone out).

**[IMPLEMENTED]** Password bootstrap: on first startup, if no password hash exists yet, `TEACHER_INITIAL_PASSWORD` (from `.env`) is hashed and stored if present; otherwise a random password is generated, hashed, stored, and printed once to the server console. Never a hardcoded or predictable default.

**[TESTED]** All 11 distinct `/admin/*` route families confirmed to redirect (303) to `/login` when unauthenticated. Correct password grants access; incorrect password is rejected with a clear message. Logout confirmed to actually revoke access (subsequent request redirects again). Both bootstrap paths tested (`TEACHER_INITIAL_PASSWORD` present, and the random-generation fallback).

---

## 5. Password security / Settings

**[IMPLEMENTED]** `GET/POST /admin/settings` and `POST /admin/settings/password`. Requires the current password to change it; rejects a new password under 8 characters; rejects a mismatched confirmation. `AppSetting` is deliberately generic so future classroom settings (default duration, default auto-advance, AI provider) can be added without another schema change — none of those were implemented now, per the instruction not to build ahead of what's needed.

**[TESTED]** All four paths: wrong current password (rejected), mismatched confirmation (rejected), too-short new password (rejected), and a genuine successful change — confirmed the *old* password stopped working and the *new* one succeeded immediately after.

---

## 6. Student result privacy

**[IMPLEMENTED]** `Student.result_token` (new column) — a random, unguessable value generated at join time, unrelated to the sequential `student_id`. `GET /student/results/{student_id}` requires the matching token (verified with `hmac.compare_digest`, a constant-time comparison, so response timing can't leak a partial match). Every failure path — no such student, missing token, wrong token — returns the *identical* 404 response, so the error itself can't be used to enumerate valid student IDs. `_student_own_results()` is structurally scoped to a single `Student` object, so it cannot return another student's data even by future modification error.

**[TESTED]** Real attack attempts, including through an actual browser navigation (not just direct HTTP requests): ID-swap with another student's stolen-but-mismatched token → 404; no token → 404; guessed token → 404; nonexistent student ID → identical error body to a wrong-token attempt on a real student. Positively verified two students' own results pages showed correct, independently different data, with neither page mentioning the other student.

**[KNOWN LIMITATION]** There is no full student login/account system — access is entirely by possession of the unguessable link (matching the scope of what was requested: privacy against ID-manipulation, not a student authentication system).

---

## 7. Race-condition / reconnection hardening

**[IMPLEMENTED]** Two concrete bugs were found (via code audit, not assumption) and fixed:

- **Response-time anchor mismatch**: a late-arriving submission for a question the session had already advanced past was silently computing response time against the *new* current question's timer anchor, producing a nonsensical value. Fixed: response time is now only computed when the question being answered is still the session's actual current question; otherwise it's recorded as `None` (honest) rather than a fabricated number. The answer itself is still accepted and graded normally either way.
- **Concurrent duplicate submissions**: two near-simultaneous requests for the same (student, session, question) could both pass the existence pre-check before either committed, and the second would hit an unhandled `IntegrityError` against the database's unique constraint. Fixed with a try/except that treats this exactly like the ordinary "already submitted" case instead of surfacing a raw error mid-quiz.

**[IMPLEMENTED]** Tab-visibility-triggered immediate polling added to all three student-facing polling pages (question player, waiting room, waiting-for-next-question) — closes the gap where a mobile browser throttles/pauses `setInterval` while a tab is backgrounded, so state reconciles as soon as the tab becomes visible again rather than waiting for the next scheduled poll.

**[TESTED]** The late-arriving-submission scenario was reproduced directly (advance the session, *then* submit for the old question) — confirmed the answer still graded correctly while `response_time_ms` was correctly `None`. The concurrent-duplicate scenario was tested with 10 genuinely concurrent threaded HTTP requests — all returned 200, and exactly one response row existed afterward.

**[KNOWN LIMITATION]** No load/stress test beyond 10 concurrent threads was performed; behavior under substantially higher concurrency (e.g. 50+ simultaneous students) has not been measured.

---

## 8. Gemini integration — preserved, not modified

**[IMPLEMENTED]** No changes to the AI grading architecture, provider selection, or environment variable names. `google-genai`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `AI_GRADING_PROVIDER` are all untouched from before Phase 1.

**[TESTED]** Re-verified this session (not assumed from before): Short Answer Level 1 (exact match) still resolves correctly without any AI call under the new auth-gated Results page. A full `NEEDS_REVIEW → teacher review → CORRECT` flow was exercised end-to-end (via the mock provider, consistent with how this was tested previously) and confirmed to still work identically now that `/admin/results/review/{id}` sits behind the authentication middleware.

**[KNOWN LIMITATION]** As before Phase 1: a genuine live call to the real Gemini API has not been made from this development environment (outbound network here doesn't reach `generativelanguage.googleapis.com`). The mock-provider tests exercise the exact same downstream code path (confidence threshold, scoring, Results, leaderboard) as a real call would.

---

## 9. Existing MVP functionality

**[TESTED]** Re-confirmed this session: True/False grading still correct with its own per-question duration. Full route-protection sweep across every `/admin/*` route plus confirmation that public routes (`/`, `/join`, `/student`, `/login`) remain accessible without authentication. One complete real-browser lifecycle test: teacher login → quiz/question creation (MCQ single + MCQ multiple with distinct durations) → publish → session → two students joining at different times (one mid-quiz) → answering → teacher advancing → finishing → leaderboard → each student's own results → a live privacy attack blocked → zero JavaScript console errors throughout.

---

## Summary of database schema changes

| Table | Change |
|---|---|
| `questions` | + `duration_seconds` (Integer, default 600, backfilled from prior quiz duration) |
| `responses` | `score` changed `Integer` → `Float` (no migration required — verified SQLite/SQLAlchemy already handle this losslessly) |
| `students` | + `result_token` (String, backfilled for existing students) |
| *(new table)* | `app_settings` (key/value store; currently holds `teacher_password_hash`) |

All migrations are automatic on startup (`init_db()` in `database.py`), non-destructive, and were tested against a simulated pre-Phase-1 database containing real prior data.
