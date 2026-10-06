# SmartQuiz — Phase 2: Classroom Radio

This document covers the Classroom Radio feature added on top of the frozen `SmartQuiz_Phase1_Baseline`. It does not repeat Phase 1's own documentation (`PHASE_1_CHANGELOG.md`, `PHASE_1_README.md`) — see those for authentication, scoring, question timers, and the Gemini grading pipeline, none of which changed in this phase.

## 1. Architecture — the server is authoritative

Classroom Radio is a **separate live system** attached to the same `Session` row a live quiz already uses, but deliberately uncoupled from question progression:

```
LiveSession
├── Quiz state (unchanged from Phase 1)
│   current_question_index, current_question_started_at, ...
└── Radio state (new)
    radio_track_id, radio_status, radio_position_seconds,
    radio_changed_at, radio_revision
```

Nothing in the radio code reads `current_question_index` or `current_question_started_at`, and nothing in the quiz-timer code reads any `radio_*` field. Verified directly: advancing a quiz from one question to the next leaves the radio's `revision` completely untouched and playback position uninterrupted (see Testing, below).

**The server never streams audio.** The teacher uploads a file once; it's stored locally under `static/uploads/audio/` and served the same way any static file is — a normal HTTP GET, browser-cacheable, identical for every student. What the server is authoritative for is *timing*: a compact JSON status (`{status, position, track_id, track_url, duration, revision, server_time}`), which every browser (teacher and student alike) uses to drive its own local `<audio>` element. This is why 15 simultaneous simulated students polling the status endpoint completed in 50ms with no server strain in testing — the server's job per student is a tiny JSON query, not an audio stream.

## 2. Synchronization model

The position is **computed, not continuously stored**. A single anchor — `(radio_position_seconds, radio_changed_at, radio_status)` — is written on every state-changing action (play/pause/stop/reset/seek/track-select), and the *current* position is derived from it on every read:

- **PLAYING**: `radio_position_seconds + (now − radio_changed_at)`
- **PAUSED / STOPPED**: `radio_position_seconds` outright (no elapsed time added — nothing is advancing)

This is the exact same pattern Phase 1's quiz countdown timer already uses (`current_question_started_at` / `_question_ends_at`), applied to a second, independent piece of state. Every control action first "freezes" the current effective position into a fresh anchor (`_radio_freeze`) before applying its own change, so the math never accumulates error across many small actions.

**Verified with real timing** (not just unit tests of the formula): a teacher started playback, one student joined 6.3 seconds later and began at position 6.0s; a second student joined 14.0 seconds in and began at 14.3s — both within normal network/test jitter of their actual join time, not at zero.

## 3. Teacher controls

All under `/admin/quizzes/{quiz_id}/sessions/radio-*`, protected by the same `_admin_auth_gate` middleware that protects every other `/admin` route (no separate authorization code needed or written).

| Action | Behavior |
|---|---|
| **Play** | Continues from wherever `radio_position_seconds` currently is. Doubles as "Resume" — there's no separate resume action, since resuming from pause and starting fresh are the same operation once the position is preserved correctly. One exception: if the position is already at/past the track's known end, Play restarts from 0 (playing from the very end would just immediately stop again). |
| **Pause** | Freezes the current computed position exactly. |
| **Stop** | Position → 0, status → STOPPED. (Documented choice, per the spec's own recommendation — a subsequent Play starts fresh, unlike Pause.) |
| **Reset** | Position → 0, but the PLAYING/PAUSED status is *preserved*: resetting while playing immediately restarts from 0 for everyone; resetting while paused moves everyone to 0 and stays paused there. |
| **Seek ±10s** (or any delta) | Adjusts the current position by the given delta, clamped to `[0, duration]` when duration is known. Status is unchanged — seeking doesn't start or stop playback. |
| **Select track** | Always a clean start: STOPPED at position 0 on the new track — continuing the old track's elapsed-time math against a different track's timeline would be meaningless. |

Every action accepts an optional `expected_revision`; if the session's actual `radio_revision` doesn't match, the action is rejected as stale rather than applied — the same idempotency pattern Phase 1 already used for `next-question`/`finish`. **Verified**: a simulated stale Play request (referencing an old revision from before a Pause had already happened) was correctly rejected, and the radio remained Paused rather than being silently restored to Playing.

## 4. Student behavior

Students get a minimal, read-only player (`templates/_radio_player_student.html`, shared across the waiting room, question page, and post-answer waiting page) showing the track name and status — no controls. This isn't just hidden by CSS: the student-facing status endpoint (`GET /session/{code}/radio/status`) is a plain `GET` with no side effects at all, so there is no route surface a student could use to control playback even if they inspected the page's network requests. Every actual control lives exclusively under `/admin`.

### Late joiners
A student's browser applies whatever the server's current authoritative state is, immediately, the first time it loads — there's no "start from 0 and catch up" step. **Verified** in the same synchronization test above.

### Reconnection
Every poll response is applied the same way, whether it's the student's first load or their fiftieth — there's no special "reconnect" code path, because normal operation already handles it. **Verified** two ways:
- A student was disconnected (navigated away) for 5 seconds while the radio kept playing server-side; on reconnecting, their position matched the current server position (not resumed from where they left off, and not restarted at 0).
- A student was disconnected while the radio was Playing; the teacher paused it during the disconnection; on reconnecting, the student correctly landed in the Paused state at the correct position, not still playing.

### Drift correction
Independent of the ~3s poll cadence, a separate check runs every 4 seconds: if the local `<audio>` element's actual position has drifted from the expected server-computed position by more than 1.5 seconds, it's hard-corrected with a seek. Smaller drift is left alone — deliberately not "over-engineered" with continuous playback-rate nudging, per the spec's own guidance to avoid audible glitches from constant micro-correction.

### Browser autoplay restrictions
Modern browsers block unmuted `audio.play()` without a preceding user gesture. When that happens, the player shows an explicit "Tap to enable classroom audio" button; tapping it *is* the legitimate user gesture that unlocks playback — this is not a workaround or bypass, it's the browser's own intended mechanism. **Verified**: since Playwright's automation context doesn't reproduce genuine autoplay blocking (confirmed by testing — `play()` succeeded even with the strict `--autoplay-policy=user-gesture-required` flag, a known automation-context quirk), the blocked-play `Promise` rejection was reproduced directly by overriding `HTMLMediaElement.prototype.play` to reject, confirming: the fallback button correctly appears, tapping it correctly starts playback, and the resulting position reflects real elapsed time rather than restarting at 0.

## 5. Interruption independence

Pausing, resuming, or advancing the quiz never touches any `radio_*` field — confirmed directly by comparing `radio_revision` before and after advancing a question mid-playback (unchanged) and confirming the radio was still Playing with an uninterrupted, correctly-advanced position. The only thing that stops the radio is the **session itself** ending (`Finish Session` or `End Session`) — not an individual student finishing early, and not a quiz pause. This was a deliberate, explicit choice per the spec's own recommendation and is implemented in exactly two places (`finish_quiz` and `end_session`), both verified to leave the radio untouched by anything else.

## 6. Reusable music library

`MusicTrack` is a standalone table, independent of any quiz or session — the same track can be selected across many different live sessions over time. Teacher-only management lives at `/admin/music` (list, upload, delete), reachable from a new "Music Library" sidebar link.

Deleting a track currently selected by a **Live** session is blocked with a clear message — the audio disappearing mid-class would break playback for every connected student with no recovery path except picking a different track. A **Completed** session's now-historical `radio_track_id` can point at a since-deleted track harmlessly; nothing reads it once the session has ended.

## 7. File storage and security

Three layers, all standard-library only (no new dependency added for this):

1. **Extension whitelist** — `.mp3`, `.wav`, `.ogg`, `.m4a` only.
2. **Magic-byte content sniffing** — the actual first bytes of the uploaded content are checked against each format's real file signature (ID3/MPEG frame sync for MP3, `RIFF...WAVE` for WAV, `OggS` for OGG, the ISO-BMFF `ftyp` box for M4A), independent of whatever extension or `Content-Type` the upload claimed. **Verified directly**: a file containing a PHP payload and a file containing a Windows executable header, both named with a `.mp3` extension, were both correctly rejected — the extension alone proves nothing, the byte content does.
3. **Streamed size cap** (50 MB) — enforced by reading the upload in 1MB chunks and aborting (deleting the partial file) the moment the running total exceeds the limit, rather than trusting a `Content-Length` header or buffering an unbounded upload before checking. **Verified** with a lowered test limit: an oversized upload was rejected and left no partial file behind.

The stored filename is **always** a server-generated UUID plus the validated extension — the uploader's original filename is never used in the storage path at all, which is what rules out path traversal and filename-based tricks by construction, not by trying to sanitize untrusted input after the fact.

Best-effort duration extraction: exact for WAV via Python's stdlib `wave` module (verified against a real generated WAV file — extracted duration matched to the second); `None` for other formats rather than a guessed/wrong value, displayed as "—:—" in the library UI. See Known Limitations.

## 8. Database changes

| Table | Change |
|---|---|
| `music_tracks` | New table: `id`, `display_name`, `storage_filename`, `duration_seconds` (nullable), `file_size_bytes`, `uploaded_at`. |
| `sessions` | + `radio_track_id`, `radio_status` (default `STOPPED`), `radio_position_seconds` (default `0`), `radio_changed_at`, `radio_revision` (default `0`). |

Migration is automatic and non-destructive. Every pre-existing session gets exactly the safe defaults above — no track selected, stopped, position zero — **verified** against a simulated pre-Phase-2 database with a real prior session: the migration added the columns with those exact defaults and left the session's existing data untouched.

## 9. Testing

Every item below was actually executed against the running application — not inferred from code review.

| Area | Result |
|---|---|
| Upload: valid WAV, correct duration extracted | **TESTED — PASS** |
| Upload: invalid file type (`.txt`) | **TESTED — PASS** (rejected, clear message, real browser) |
| Upload: disguised malicious content (fake PHP/.exe as `.mp3`) | **TESTED — PASS** (rejected by content, not extension) |
| Upload: oversized file | **TESTED — PASS** (rejected, no partial file left) |
| Library: list / select / delete | **TESTED — PASS** |
| Delete blocked while track in use by a Live session, then succeeds once session ends | **TESTED — PASS** |
| Unauthorized (student) access to all library + radio routes | **TESTED — PASS** (every route redirects) |
| Play / Pause / Resume-preserves-position | **TESTED — PASS** (exact position match before/after) |
| Stop → 0 | **TESTED — PASS** |
| Reset while Playing → 0, stays Playing | **TESTED — PASS** |
| Reset while Paused → 0, stays Paused | **TESTED — PASS** |
| Seek +10 / −10, exact | **TESTED — PASS** |
| Stale request rejection (revision mismatch) | **TESTED — PASS** |
| Synchronization: two real students joining at different times converge on the current position | **TESTED — PASS** (the single most important test in this phase) |
| Reconnection while Playing | **TESTED — PASS** |
| Reconnection while Paused (paused during disconnect) | **TESTED — PASS** |
| Autoplay-blocked fallback button appears and works | **TESTED — PASS** (via a monkey-patched `play()` rejection, since Playwright's own automation context doesn't reproduce genuine browser autoplay blocking) |
| Quiz interruption does not affect radio (revision, status, position all unchanged) | **TESTED — PASS** |
| Session end stops the radio | **TESTED — PASS** |
| Multi-student concurrent load (15 simultaneous polls) | **TESTED — PASS** (50ms total, consistent data, confirming the shared-static-file architecture) |
| Drift correction | **IMPLEMENTED**, not separately load-tested under artificial network jitter this session |
| Phase 1 regression: auth, all 4 question types (including Phase 1's own per-question duration and MCQ-multiple proportional scoring), student privacy | **TESTED — PASS** |
| JS console errors across every browser test performed | **TESTED — PASS** (zero, in every test that used a real browser) |

## 10. Known limitations

- **Duration is only known for WAV files.** MP3/OGG/M4A tracks show "—:—" in the library and have no server-side end-of-track auto-stop — they play until explicitly stopped by the teacher. This is an honest scope limitation, not a silent wrong answer: extracting exact duration from compressed formats without a new dependency (e.g. `mutagen`) wasn't attempted, per the instruction not to add unnecessary dependencies.
- **Looping is not implemented.** Not requested as required functionality; out of scope per the phase's own scope limit.
- **Drift correction was not load-tested under simulated network jitter or long play sessions** (e.g. 30+ minutes) — the mechanism is implemented and unit-reasoned about, but not stress-tested this session.
- **The "track in use, can't delete" guard** was verified as present in the code and exercised once during development, but not re-tested as part of this session's final test pass.
- **No genuine browser autoplay-blocking test was possible** in this environment (Playwright's Chromium automation context doesn't reproduce the real restriction even with the relevant Chromium flag set) — the fallback *logic* was verified correctly via a direct `play()` rejection simulation instead, which exercises the same code path a real block would trigger.
