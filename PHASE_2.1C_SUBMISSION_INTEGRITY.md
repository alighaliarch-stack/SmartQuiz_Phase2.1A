# Phase 2.1C — Exam Mode Submission Integrity

Exam Mode is an explicit per-quiz option. It defaults off, so existing quizzes
continue using Classroom Mode unless a teacher enables it.

## Authoritative submission flow

For every quiz mode, the student submission endpoint reloads the addressed
session under a SQLite write reservation and rejects submissions if that
specific session is no longer Live. This also serializes Classroom Mode
submissions against teacher session-ending actions. For an Exam Mode quiz the
endpoint then reloads the student, quiz, question, and effective per-student
progress before making a decision. Client clocks and the posted
`question_displayed_at` value are not used for enforcement.

The server rejects a submission if the session has ended, the student is not in
the session, the question does not belong to the session quiz, the student has
completed the quiz, or the submitted question is not the student's effective
current question. The deadline is the authoritative question start timestamp
plus that question's `duration_seconds`; receipt at the deadline is accepted,
and receipt after it is rejected. A late rejection creates no Response and
awards no score.

The Response, per-student Auto Advance update, and accepted audit event are
committed together. In Exam Mode, Auto Advance occurs on an accepted submission
or server-detected expiry. An expiry advances that student's timer from the
previous deadline without creating a Response; a late attempt against that
personally expired question is still audited as `SUBMISSION_EXPIRED`. Without
Auto Advance, an expired unanswered question remains current until teacher
progression. A final expired question remains unanswered. Classroom Mode keeps
its existing timeout-submission behavior. A completed session remains ended
for status reads even when its parent quiz is republished or reused for a new
session; each LiveSession's own status is authoritative whenever that session
is evaluated.

The v2.1c.1 corrective release applies these lifecycle checks without a schema
change. It preserves the Exam Mode submission-integrity protections described
here.

## Audit events

`SubmissionEvent` rows record the server timestamp and receipt time, session,
student, quiz, question ID/number, decision code, authoritative question ID,
deadline, and Response ID when one exists. Submission events use the receipt time of the POST request.
`QUESTION_EXPIRED_UNANSWERED` records use the receipt time of the status poll
that triggered per-student timeout progression; their question fields identify
the timed-out question, not an answer submission. These system events are
committed with that progression; the final question, which does not advance,
does not generate this progression event. IDs are stored without foreign keys
so rejected invalid or cross-quiz identifiers remain auditable. Teacher
results show recent events (up to 200).

Late answer attempts (`SUBMISSION_EXPIRED`) are therefore distinguishable
from an unanswered timeout (`QUESTION_EXPIRED_UNANSWERED`), and neither
creates a Response.

Decision codes include:

- `SUBMISSION_ACCEPTED`
- `SUBMISSION_ALREADY_SUBMITTED`
- `SUBMISSION_EXPIRED`
- `SUBMISSION_NOT_CURRENT`
- `SUBMISSION_WRONG_SESSION_QUESTION`
- `SUBMISSION_SESSION_ENDED`
- `SUBMISSION_STUDENT_COMPLETED`
- `SUBMISSION_STUDENT_NOT_IN_SESSION`
- `QUESTION_EXPIRED_UNANSWERED`

## Schema initialization

Database initialization adds the non-null `quizzes.exam_mode` column with a
false default to an existing database. SQLAlchemy metadata creates the
`submission_events` table and its indexes on fresh initialization. No existing
Response rows or historical question snapshots are rewritten.

## Final verification and release validation

The final-current-code verification was run after the final timeout-event,
duplicate-order, expiry, and teacher-progression refinements; these results
refer to that final implementation, not an earlier intermediate state:

- Broad regression: **29 PASS / 0 FAIL**. Covered submission decisions,
  deadline boundaries, concurrency, teacher/student ordering, Auto Advance,
  Phase 2.1A identity/reconnect, Classroom Radio, quiz reuse, historical
  snapshots, teacher authentication, result-token privacy, fresh schema,
  migration, SQLite integrity, and live Exam Mode question-mutation attempts.
- Focused regression: **11 PASS / 0 FAIL**. Covered late, exact-deadline, and
  timely submissions; future-question and cross-quiz rejection; duplicates;
  timeout audit with no fabricated Response; teacher progression races;
  concurrent duplicate submissions; multi-student submissions; and
  completed-session rejection.

Live Exam Mode mutation checks confirmed active sessions reject the tested
question mutations with HTTP 409: current and future question edits, question
deletion, addition, duplication, duration changes, and answer/options/points
changes. No question content or question-set mutation occurred in these
checks.
