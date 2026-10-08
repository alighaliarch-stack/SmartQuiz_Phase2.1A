# SmartQuiz v2.1c.1 - Corrective Release

This release corrects two session-lifecycle defects confirmed against v2.1c:

- A supplied `LiveSession` is now authoritative when determining its phase.
  Completed sessions remain `SESSION_ENDED` after their parent quiz is
  republished or reused.
- Classroom Mode rejects submissions after the addressed session ends. The
  ended-session check is serialized against teacher lifecycle actions and
  occurs before answer, score, progress, or completion changes.

No database schema change was required. Exam Mode's existing authoritative
identity, question, deadline, duplicate-submission, expiry, audit, Auto Advance,
race-safety, mutation-protection, and reconnect protections are preserved.
