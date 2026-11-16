"""
models.py
---------
SQLAlchemy ORM models live here.

`Quiz` — basic metadata (title, description, duration, passing
percentage, created date) plus a `status` lifecycle:

    Draft -> Published -> Live -> Completed -> Archived

  - Draft:      just created, not visible to students.
  - Published:  teacher marked it ready via "Publish Quiz" in the
                Question Builder. Now eligible to appear on the
                Live Sessions page, but students still can't join yet.
  - Live:       a Session has been started for it (see below). Students
                can join at /join or /join/{session_code} while this lasts.
  - Completed:  the teacher clicked "End Session".
  - Archived:   reserved for a future manual archive action — nothing
                sets this yet.

`duration_minutes` is now PURELY the quiz's overall/legacy duration shown
in the dashboard/builder (e.g. "5 min" quiz summary) — it is NOT the
active countdown source. Each Question carries its own
`duration_seconds` (15-1200 range), and that is what the shared
countdown timer is actually built from (see _question_ends_at in
app.py). This replaced the earlier "quiz duration doubles as every
question's countdown" design once Phase 1 required independent
per-question timing. `duration_minutes` is preserved rather than
removed since existing quizzes/UI still reference it as a summary
figure and there's no reason to force a breaking change there.

`auto_advance` controls what happens when a question's timer hits
zero: if enabled, the Live Sessions page automatically clicks
Next Question (or Finish Quiz, on the last one) on the teacher's
behalf; if disabled, the teacher still has to click it manually.

`Question` — belongs to a Quiz. Supports four types (mcq_single,
mcq_multiple, true_false, short_answer). `options` and `correct_answer`
are stored as JSON text so the same two columns work across all four
types without a sprawling set of type-specific columns.

`Student` — a participant who joined a quiz. `session_id` is set when
they join while a specific Session is live, so "connected students" can
be counted per live session rather than per quiz's whole history.
`result_token` is a random, unguessable identifier (NOT the same as the
sequential `id`) required to view that student's own results page —
this is what stops a student from seeing another student's results by
simply incrementing a student_id in the URL, without needing a full
student login/authentication system.

`Session` — one live run of a quiz: a random 6-character join code,
when it started/ended, and its own status (Live / Completed). A quiz
can have many sessions over time (e.g. re-run across different class
periods), each with a different code. `current_question_started_at` is
set fresh every time `current_question_index` changes (Start Quiz /
Next Question) — it's the shared clock every student's and the
teacher's countdown timer is computed from, so everyone sees the same
countdown for a given question regardless of when they personally
loaded the page. The countdown LENGTH now comes from the CURRENT
question's own `duration_seconds`, not a quiz-wide value.

`AppSetting` — a minimal key/value store for small pieces of
application state that don't warrant their own table: currently just
the teacher's password hash, but deliberately generic so future
classroom settings (default question duration, default auto-advance,
etc.) can reuse the same mechanism without another schema change.
"""

import random
import secrets
import string
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from database import Base


class Quiz(Base):
    __tablename__ = "quizzes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="", nullable=False)
    duration_minutes = Column(Integer, default=10, nullable=False)
    passing_percentage = Column(Integer, default=60, nullable=False)
    status = Column(String(20), default="Draft", nullable=False)
    auto_advance = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    questions = relationship(
        "Question",
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="Question.id",
    )
    students = relationship(
        "Student",
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="Student.connected_at",
    )
    sessions = relationship(
        "Session",
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="Session.started_at.desc()",
    )


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False, index=True)
    question_text = Column(Text, nullable=False)
    question_type = Column(String(20), nullable=False, default="mcq_single")
    points = Column(Integer, default=1, nullable=False)
    options = Column(Text, default="[]", nullable=False)          # JSON-encoded
    correct_answer = Column(Text, default="null", nullable=False)  # JSON-encoded
    image_path = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # The countdown length for THIS question specifically, in seconds.
    # Valid range 15-1200 (20 min), enforced in app.py's validation, not
    # at the DB layer (SQLite has no CHECK-constraint enforcement worth
    # relying on here, and the friendlier place to reject an invalid
    # value is the form handler, before it ever reaches the DB). Default
    # of 600s (10 min) matches the prior quiz-wide default this replaces.
    duration_seconds = Column(Integer, default=600, nullable=False)

    # Short Answer only. Optional free-text grading guidance — required
    # concepts, accepted synonyms, what should/shouldn't count as correct
    # — passed to the AI evaluator (Level 3) as extra context when
    # present. Deliberately a single free-text field rather than several
    # structured ones (required-concepts / synonyms / optional-examples
    # as separate inputs): the AI already parses natural language well,
    # so one flexible field covers the same ground without a heavier
    # question-authoring UI. NULL/empty for every other question type,
    # and for existing Short Answer questions created before this field
    # existed — the grading pipeline works fine without it, since the
    # deterministic layers (see _grade_short_answer in app.py) derive
    # required-vs-optional concepts from the correct_answer text itself.
    grading_notes = Column(Text, nullable=True)

    quiz = relationship("Quiz", back_populates="questions")


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    name = Column(String(150), nullable=False)
    roll_number = Column(String(50), nullable=False)
    course = Column(String(100), default="", nullable=False)
    section = Column(String(50), default="", nullable=False)
    connected_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    status = Column(String(20), default="Ready", nullable=False)  # Ready | Disconnected | Finished
    # Random, unguessable (NOT the sequential `id`) — required to view
    # this student's own results page. See the module docstring.
    result_token = Column(String(43), nullable=True, index=True)

    # Per-student question progression, used ONLY when the quiz's
    # auto_advance is True (see _student_effective_question_index in
    # app.py). When auto_advance is False, these are never read or
    # written — that mode's navigation is driven entirely by the
    # existing global Session.current_question_index /
    # current_question_started_at fields, exactly as before this was
    # added, so nothing about the teacher-controlled synchronized flow
    # changes.
    #
    # 0 means "hasn't personally started the quiz yet" (still in the
    # waiting room, or the quiz hasn't been started at all). Set to 1
    # the first time this student needs to be on a question (whether
    # they were already waiting when Start Quiz was clicked, or they
    # join after it already started — either way their own personal
    # pacing begins at that moment, with a full-length timer for
    # Question 1). Incremented by 1 immediately when this student
    # submits an answer, or when their own personal timer for the
    # current question expires — never waiting on any other student or
    # on the global session timer.
    current_question_index = Column(Integer, default=0, nullable=False)
    question_started_at = Column(DateTime, nullable=True)

    # Stable student identity for learning content / flashcards. The
    # legacy quiz participation fields remain in place for the existing
    # classroom flow; this adds the additional persistent identity and
    # assignment metadata needed for course/module access and review
    # history without requiring a separate, parallel user table.
    student_code = Column(String(32), unique=True, nullable=True, index=True)
    external_id = Column(String(100), nullable=True, index=True)
    email = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    quiz = relationship("Quiz", back_populates="students")
    session = relationship("Session", back_populates="students")
    enrollments = relationship("StudentEnrollment", back_populates="student", cascade="all, delete-orphan")
    flashcard_progress = relationship("StudentFlashcardProgress", back_populates="student", cascade="all, delete-orphan")
    flashcard_reviews = relationship("FlashcardReview", back_populates="student", cascade="all, delete-orphan")


class Session(Base):
    """One live run of a quiz. `current_question_index` drives the whole
    multi-question flow: 0 means the session is open for joining but the
    teacher hasn't clicked "Start Quiz" yet; 1..N means students should
    be on question N; and once the teacher clicks "Finish Quiz" it's set
    to len(questions) + 1, which is how students/teacher distinguish a
    natural "all questions done" completion from an early manual
    End Session (still <= total questions) — no extra status value
    needed for that distinction.

    Classroom Radio (Phase 2) is a SEPARATE live system attached to the
    same Session row, deliberately not coupled to question progression
    at all — a session's radio_* fields are read/written independently
    of current_question_index and current_question_started_at.

      radio_track_id            the MusicTrack currently selected for
                                 this session (nullable — no track
                                 selected yet)
      radio_status               STOPPED | PLAYING | PAUSED
      radio_position_seconds     the authoritative position, in
                                 seconds, AS OF radio_changed_at — not
                                 continuously updated in the database
                                 while playing (that would mean writing
                                 to SQLite every second for every live
                                 session, for no benefit)
      radio_changed_at           server UTC timestamp of the last
                                 state-changing action (play/pause/
                                 stop/reset/seek/track change)
      radio_revision             increments on every state-changing
                                 action; used to reject stale/out-of-
                                 order requests and responses (see
                                 _radio_effective_state in app.py)

    The CURRENT position at any instant is computed, not stored:
      - if radio_status == PLAYING:
            radio_position_seconds + (now - radio_changed_at)
      - if PAUSED or STOPPED:
            radio_position_seconds (frozen — no elapsed time added)
    This is the same "position + elapsed_server_time" model, and the
    same reason, as the quiz's own current_question_started_at /
    _question_ends_at timer: a single server-side anchor that every
    client computes its local countdown/position FROM, rather than the
    server pushing continuous updates.
    """

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False, index=True)
    session_code = Column(String(6), unique=True, nullable=False, index=True)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    ended_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="Live", nullable=False)  # Live | Completed
    current_question_index = Column(Integer, default=0, nullable=False)
    current_question_started_at = Column(DateTime, nullable=True)

    radio_track_id = Column(Integer, ForeignKey("music_tracks.id"), nullable=True)
    radio_status = Column(String(10), default="STOPPED", nullable=False)
    radio_position_seconds = Column(Float, default=0.0, nullable=False)
    radio_changed_at = Column(DateTime, nullable=True)
    radio_revision = Column(Integer, default=0, nullable=False)

    quiz = relationship("Quiz", back_populates="sessions")
    students = relationship("Student", back_populates="session")
    radio_track = relationship("MusicTrack")


class Course(Base):
    """Teacher-managed course/class container for learning modules."""

    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    code = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    modules = relationship("LearningModule", back_populates="course", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="course", cascade="all, delete-orphan")
    enrollments = relationship("StudentEnrollment", back_populates="course", cascade="all, delete-orphan")


class LearningModule(Base):
    """A teacher-defined unit inside a course with documents and generated content."""

    __tablename__ = "learning_modules"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="", nullable=False)
    learning_objectives = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    course = relationship("Course", back_populates="modules")
    documents = relationship("Document", back_populates="module", cascade="all, delete-orphan")
    flashcards = relationship("Flashcard", back_populates="module", cascade="all, delete-orphan")
    generated_questions = relationship("GeneratedQuestion", back_populates="module", cascade="all, delete-orphan")
    enrollments = relationship("StudentEnrollment", back_populates="module", cascade="all, delete-orphan")


class Document(Base):
    """Uploaded source document for one module, with immutable version evidence."""

    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    module_id = Column(Integer, ForeignKey("learning_modules.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    original_filename = Column(String(255), nullable=False)
    storage_filename = Column(String(255), nullable=False, unique=True)
    mime_type = Column(String(100), nullable=False)
    file_extension = Column(String(20), nullable=False)
    file_size_bytes = Column(Integer, default=0, nullable=False)
    extraction_status = Column(String(30), default="PENDING", nullable=False)
    extraction_error = Column(Text, nullable=True)
    extracted_text = Column(Text, nullable=True)
    is_archived = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    course = relationship("Course", back_populates="documents")
    module = relationship("LearningModule", back_populates="documents")
    versions = relationship("DocumentVersion", back_populates="document", cascade="all, delete-orphan")


class DocumentVersion(Base):
    """Versioned snapshot of a document used for source traceability."""

    __tablename__ = "document_versions"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    version_number = Column(Integer, default=1, nullable=False)
    original_filename = Column(String(255), nullable=False)
    storage_filename = Column(String(255), nullable=False)
    mime_type = Column(String(100), nullable=False)
    sha256 = Column(String(128), nullable=False)
    extraction_status = Column(String(30), default="PENDING", nullable=False)
    extraction_error = Column(Text, nullable=True)
    extracted_text = Column(Text, nullable=True)
    is_current = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    document = relationship("Document", back_populates="versions")


class StudentEnrollment(Base):
    """Explicit course/module assignment; no shared URL-only access is allowed."""

    __tablename__ = "student_enrollments"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    module_id = Column(Integer, ForeignKey("learning_modules.id"), nullable=True, index=True)
    status = Column(String(20), default="Active", nullable=False)
    assigned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    expires_at = Column(DateTime, nullable=True)

    student = relationship("Student", back_populates="enrollments")
    course = relationship("Course", back_populates="enrollments")
    module = relationship("LearningModule", back_populates="enrollments")
    flashcard_progress = relationship("StudentFlashcardProgress", back_populates="enrollment", cascade="all, delete-orphan")


class Flashcard(Base):
    """Teacher-approved flashcard content generated from a source document."""

    __tablename__ = "flashcards"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("learning_modules.id"), nullable=False, index=True)
    source_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True, index=True)
    source_document_version_id = Column(Integer, ForeignKey("document_versions.id"), nullable=True, index=True)
    front = Column(Text, nullable=False)
    back = Column(Text, nullable=False)
    explanation = Column(Text, default="", nullable=False)
    topic = Column(String(150), default="", nullable=False)
    difficulty = Column(String(20), default="medium", nullable=False)
    source_reference = Column(String(255), default="", nullable=False)
    approval_status = Column(String(20), default="DRAFT", nullable=False)
    generated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    ai_provider = Column(String(30), nullable=True)
    ai_model = Column(String(50), nullable=True)

    module = relationship("LearningModule", back_populates="flashcards")
    progress = relationship("StudentFlashcardProgress", back_populates="flashcard", cascade="all, delete-orphan")
    reviews = relationship("FlashcardReview", back_populates="flashcard", cascade="all, delete-orphan")


class GeneratedQuestion(Base):
    """Teacher-reviewed generated quiz question sourced from a module document."""

    __tablename__ = "generated_questions"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("learning_modules.id"), nullable=False, index=True)
    source_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True, index=True)
    source_document_version_id = Column(Integer, ForeignKey("document_versions.id"), nullable=True, index=True)
    question_text = Column(Text, nullable=False)
    question_type = Column(String(20), default="mcq_single", nullable=False)
    answer_options = Column(Text, default="[]", nullable=False)
    correct_answer = Column(Text, default="null", nullable=False)
    explanation = Column(Text, default="", nullable=False)
    points_suggestion = Column(Integer, default=1, nullable=False)
    difficulty = Column(String(20), default="medium", nullable=False)
    topic = Column(String(150), default="", nullable=False)
    source_reference = Column(String(255), default="", nullable=False)
    approval_status = Column(String(20), default="DRAFT", nullable=False)
    generated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    ai_provider = Column(String(30), nullable=True)
    ai_model = Column(String(50), nullable=True)

    module = relationship("LearningModule", back_populates="generated_questions")


class StudentFlashcardProgress(Base):
    """Review progress for one student/flashcard combination."""

    __tablename__ = "student_flashcard_progress"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    flashcard_id = Column(Integer, ForeignKey("flashcards.id"), nullable=False, index=True)
    enrollment_id = Column(Integer, ForeignKey("student_enrollments.id"), nullable=True, index=True)
    due_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    interval_days = Column(Integer, default=1, nullable=False)
    ease_factor = Column(Float, default=2.5, nullable=False)
    review_count = Column(Integer, default=0, nullable=False)
    last_rating = Column(String(20), nullable=True)
    last_reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    student = relationship("Student", back_populates="flashcard_progress")
    flashcard = relationship("Flashcard", back_populates="progress")
    enrollment = relationship("StudentEnrollment", back_populates="flashcard_progress")


class FlashcardReview(Base):
    """History for one student review action."""

    __tablename__ = "flashcard_reviews"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    flashcard_id = Column(Integer, ForeignKey("flashcards.id"), nullable=False, index=True)
    rating = Column(String(20), nullable=False)
    interval_days = Column(Integer, default=1, nullable=False)
    next_due_at = Column(DateTime, nullable=False)
    reviewed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    student = relationship("Student", back_populates="flashcard_reviews")
    flashcard = relationship("Flashcard", back_populates="reviews")


class MusicTrack(Base):
    """The reusable Classroom Radio library (Phase 2) — teacher-managed,
    independent of any single quiz or live session. A Session selects
    ONE MusicTrack (via Session.radio_track_id) to be its active radio
    track; the same track can be reused across many different sessions
    over time. Deleting a track that's currently selected by a Live
    session is blocked (see the /admin/music delete route) so an active
    classroom's playback doesn't silently break.

    The audio file itself lives on disk under static/uploads/ (the same
    controlled upload directory question images already use), named by
    storage_filename — a random, server-generated name, never the
    user's original filename, so the stored path never depends on
    anything the uploader supplied (rules out path traversal and
    filename-based tricks by construction, not by sanitizing untrusted
    input after the fact).
    """

    __tablename__ = "music_tracks"

    id = Column(Integer, primary_key=True, index=True)
    display_name = Column(String(200), nullable=False)
    storage_filename = Column(String(255), nullable=False, unique=True)
    # Best-effort — exact for WAV (computed from the file's own header via
    # Python's stdlib `wave` module), None for other formats rather than
    # a wrong guess. See _extract_wav_duration in app.py.
    duration_seconds = Column(Float, nullable=True)
    file_size_bytes = Column(Integer, nullable=False)
    uploaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class Response(Base):
    """One student's answer to one question, scoped to a specific live
    session attempt:

      - score: the question's own `points` value if is_correct else 0
        (weighted scoring — a 3-point question correctly answered earns
        3, not a flat amount). This is the competition score; there is
        no separate aggregate "quiz score" field anywhere else — total
        score for an attempt is always the sum of its Responses' score.
      - response_time_ms: submitted_at minus session.current_question_
        started_at — the same server-only timer anchor the shared
        countdown/auto-advance is built from (see _question_ends_at /
        _compute_response_time_ms in app.py). Not derived from any
        client-supplied timestamp.

    The (student_id, session_id, question_id) unique constraint is the
    DB-level backstop against duplicate submissions WITHIN one attempt.
    It's deliberately session-scoped rather than just (student_id,
    question_id): a student can join the same quiz again in a different
    live session (e.g. a re-run competition) and that must be a fully
    independent attempt, not blocked as "already submitted" by answers
    from a previous session. In practice every join already creates a
    fresh Student row tied to that session (see /join), so student_id
    alone already disambiguates attempts today — but making session_id
    part of the constraint itself means correctness doesn't silently
    depend on that incidental fact; it's true by construction.

    The app also checks before inserting so a duplicate attempt gets a
    clean "already submitted" response instead of a database error.
    """

    __tablename__ = "responses"
    __table_args__ = (
        UniqueConstraint(
            "student_id", "session_id", "question_id",
            name="uq_response_student_session_question",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    selected_option = Column(Text, nullable=True)  # raw option text the student chose
    is_correct = Column(Boolean, default=False, nullable=False)
    # True only for FULL marks (score == question.points) — a partial-
    # credit mcq_multiple response (see _score_mcq_multiple in app.py)
    # is NOT is_correct, matching how "number of fully correct answers"
    # is used as a leaderboard tie-breaker. Float, not Integer: mcq_multiple
    # partial credit is proportional (e.g. 66.67 out of 100), and storing
    # a lossily-rounded integer would compound rounding error when scores
    # are summed across many questions.
    score = Column(Float, default=0, nullable=False)
    response_time_ms = Column(Integer, nullable=True)   # submitted_at - session.current_question_started_at
    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Short Answer grading pipeline fields. NULL for every other question
    # type (mcq_single / mcq_multiple / true_false), which are graded
    # deterministically by _check_answer_correct and have no ambiguity
    # for these fields to describe.
    #   evaluation_method:     "exact" | "keyword" | "ai" | "teacher_review" | None
    #                          ("teacher_review" is set when a teacher
    #                          manually resolves a NEEDS_REVIEW response)
    #   evaluation_status:     "CORRECT" | "INCORRECT" | "NEEDS_REVIEW"
    #                          | "EVALUATING" (AI call in flight) | None
    #   evaluation_confidence: the AI's 0.0-1.0 confidence, only set
    #                          when evaluation_method == "ai"
    #   evaluation_reason:     one-sentence explanation from the AI, or
    #                          an internal note (e.g. "AI unavailable")
    #                          when evaluation falls back to NEEDS_REVIEW
    evaluation_method = Column(String(20), nullable=True)
    evaluation_status = Column(String(20), nullable=True)
    evaluation_confidence = Column(Float, nullable=True)
    evaluation_reason = Column(Text, nullable=True)

    # Historical snapshot of the question AS IT WAS when this response
    # was submitted — not a live reference. Question rows are mutable
    # (a teacher can edit one to prepare a new session), but a past
    # session's results must keep showing exactly what that student
    # actually saw and answered, not whatever the question has since
    # been edited to say. Populated once, at submission time, in
    # app.py's session_question_submit; Results/needs-review rendering
    # prefers these fields over the live Question object.
    #
    # Nullable because responses created before this field existed have
    # no snapshot to fall back on — those old rows keep reading the live
    # Question object, same as before this fix (a known, unavoidable
    # limitation for pre-existing data, not a bug: there was never a
    # captured snapshot for them to read in the first place).
    question_text_snapshot = Column(Text, nullable=True)
    question_type_snapshot = Column(String(20), nullable=True)
    question_options_snapshot = Column(Text, nullable=True)       # JSON-encoded
    correct_answer_snapshot = Column(Text, nullable=True)         # JSON-encoded
    question_points_snapshot = Column(Integer, nullable=True)


class AppSetting(Base):
    """Generic key/value store for small pieces of app state — currently
    only `teacher_password_hash`, but deliberately generic so future
    settings (default question duration, default auto-advance, etc.)
    reuse this instead of another one-off table/migration each time."""

    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)


def generate_session_code() -> str:
    """A random 6-character code like 'A7D92Q' — uppercase letters + digits."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choices(alphabet, k=6))


def generate_result_token() -> str:
    """A random, unguessable token for a student's own results link —
    URL-safe, ~43 chars for 32 bytes of entropy (secrets.token_urlsafe's
    usual output length), deliberately NOT derived from or related to
    the student's sequential id."""
    return secrets.token_urlsafe(32)
