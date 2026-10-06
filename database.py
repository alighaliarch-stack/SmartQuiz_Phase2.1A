"""
database.py
------------
SQLAlchemy engine, session factory, and declarative Base for SmartQuiz.

init_db() creates the SQLite file (if it doesn't exist yet) and any
tables registered on Base, then runs a small set of auto-migrations for
columns that newer model versions expect but an existing SQLite file
(created before those columns existed) might not have yet.

This project doesn't use Alembic — for a single-file SQLite app at this
scale, targeted ALTER TABLE statements are simpler, dependency-free, and
still get run automatically on every startup, same spirit as Alembic's
"upgrade head" but with no extra tooling to configure.
"""

import secrets
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from config import settings

# check_same_thread=False is required for SQLite when the same connection
# pool is shared across FastAPI's request-handling threads.
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}  # row[1] is the column name in PRAGMA table_info output


def _existing_tables(conn) -> set[str]:
    rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    return {row[0] for row in rows}


def normalize_roll_number(raw: str | None) -> str:
    """Normalizes a roll number for IDENTITY COMPARISON ONLY (Phase
    2.1A) — never for display; `Student.roll_number` always keeps the
    student's original, unmodified text. Strips leading/trailing
    whitespace, collapses any run of internal whitespace to a single
    space, and lowercases the result. Nothing else is touched: digits,
    letters, hyphens, slashes, leading zeros — every other character a
    real roll number might use is preserved exactly, so e.g.
    "23-BBA-001" and "23bba001" are correctly NOT the same identity,
    only case/whitespace variants of literally the same text are.

    Returns "" for None or an all-whitespace input. Callers must treat
    an empty normalized value as having NO comparable identity — never
    match two students against each other by an empty string (see
    join_submit in app.py, and the partial unique index below, which
    excludes '' from the uniqueness check for the same reason).
    """
    if not raw:
        return ""
    return " ".join(raw.strip().split()).lower()


def _unique_index_column_sets(conn, table: str) -> list[set[str]]:
    """Every UNIQUE constraint/index on a table, as a list of column-name
    sets — used to detect which version of a constraint is currently in
    place without relying on fragile string-matching of raw DDL text."""
    indexes = conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
    result = []
    for idx in indexes:
        # PRAGMA index_list row shape: (seq, name, unique, origin, partial)
        is_unique = idx[2]
        if not is_unique:
            continue
        idx_name = idx[1]
        cols = conn.execute(text(f"PRAGMA index_info({idx_name})")).fetchall()
        # PRAGMA index_info row shape: (seqno, cid, name)
        result.append({c[2] for c in cols})
    return result


def _migrate_response_session_uniqueness(conn) -> None:
    """The `responses` table's original UNIQUE constraint was
    (student_id, question_id) — too coarse once a student can attempt
    the same quiz across multiple live sessions of it (a re-run
    competition). This rebuilds the table with a
    (student_id, session_id, question_id) constraint instead, safely
    preserving every existing row — no data is deleted.

    SQLite has no ALTER TABLE for changing a UNIQUE constraint, so this
    follows SQLite's own documented approach for schema changes it
    doesn't support directly: build a correctly-shaped new table, copy
    every row across unchanged, then swap it in for the old one.

    Detection is via PRAGMA index introspection (not string-matching
    the table's DDL text), so this is safe to run on every startup —
    once the new constraint is in place, it's recognized as such and
    this becomes a no-op.
    """
    tables = _existing_tables(conn)
    if "responses" not in tables:
        return  # brand new DB — create_all() already built the correct schema

    unique_sets = _unique_index_column_sets(conn, "responses")
    already_correct = any(
        cols == {"student_id", "session_id", "question_id"} for cols in unique_sets
    )
    if already_correct:
        return

    has_old_constraint = any(cols == {"student_id", "question_id"} for cols in unique_sets)
    if not has_old_constraint:
        # No recognized constraint at all — don't guess, leave it alone.
        return

    conn.execute(text("""
        CREATE TABLE responses_migrated (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            session_id INTEGER,
            quiz_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_option TEXT,
            is_correct BOOLEAN NOT NULL DEFAULT 0,
            score INTEGER NOT NULL DEFAULT 0,
            response_time_ms INTEGER,
            submitted_at DATETIME NOT NULL,
            CONSTRAINT uq_response_student_session_question
                UNIQUE (student_id, session_id, question_id)
        )
    """))
    conn.execute(text("""
        INSERT INTO responses_migrated
            (id, student_id, session_id, quiz_id, question_id, selected_option,
             is_correct, score, response_time_ms, submitted_at)
        SELECT id, student_id, session_id, quiz_id, question_id, selected_option,
               is_correct, score, response_time_ms, submitted_at
        FROM responses
    """))
    conn.execute(text("DROP TABLE responses"))
    conn.execute(text("ALTER TABLE responses_migrated RENAME TO responses"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_student_id ON responses(student_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_session_id ON responses(session_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_quiz_id ON responses(quiz_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_question_id ON responses(question_id)"))


def _student_duplicate_identity_groups(conn) -> list[tuple]:
    """Every (session_id, normalized_roll_number) pair that already has
    MORE THAN ONE Student row — i.e. duplicate attempts created by the
    pre-Phase-2.1A code, which had no identity check on join at all.
    Deliberately excludes a NULL session_id and an empty/NULL
    normalized roll number from the check: those already can't collide
    under the unique index this feeds (see
    _migrate_student_session_roll_uniqueness), so they're not
    "duplicates" under the new rule even if several such rows exist."""
    rows = conn.execute(text("""
        SELECT session_id, normalized_roll_number, COUNT(*) AS c
        FROM students
        WHERE session_id IS NOT NULL
          AND normalized_roll_number IS NOT NULL
          AND normalized_roll_number != ''
        GROUP BY session_id, normalized_roll_number
        HAVING COUNT(*) > 1
    """)).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def _migrate_student_session_roll_uniqueness(conn) -> None:
    """The database-level backstop for Phase 2.1A's identity rule: the
    same roll number can only ever have ONE Student row per live
    session. This is what actually stops a concurrent race (two
    near-simultaneous join requests for the same session + roll both
    passing the pre-insert lookup in app.py's join_submit before either
    commits) from producing two rows — the application-level check
    alone cannot close that window; only a database constraint can.

    A plain UNIQUE INDEX (rather than a table-level UNIQUE CONSTRAINT,
    which SQLite can only add by rebuilding the whole table — see
    _migrate_response_session_uniqueness above for how invasive that is
    when it's genuinely needed) gives the identical guarantee here
    without rebuilding `students`, a table every Response row
    references by foreign key. It's a PARTIAL index (`WHERE
    normalized_roll_number != ''`) so a row with no comparable identity
    — a blank/whitespace-only roll number — never collides with another
    such row; SQLite's own NULL-is-distinct behavior already excludes a
    NULL session_id or NULL normalized_roll_number from the check
    without any extra clause needed for those.

    SAFETY — pre-existing duplicates: a database that ran the OLD
    join_submit (no identity check at all) may already contain more
    than one Student row for the same (session_id,
    normalized_roll_number). Creating a UNIQUE index over data that
    already violates it would fail outright, and silently deleting or
    merging those old rows to make room for it is exactly the kind of
    data loss this migration must never cause. So duplicates are
    detected FIRST: if any exist, the index is skipped entirely for
    this run, a clear diagnostic is printed naming exactly which
    (session_id, roll) pairs are affected, and every row is left
    completely untouched — historical data is preserved, full stop.
    New joins are still protected regardless, by the application-level
    check in join_submit. This detection re-runs on every startup, so
    the very next restart after those old duplicates are resolved
    (manually, by a teacher/admin decision — never automatically)
    creates the index with no further action required.
    """
    if "students" not in _existing_tables(conn):
        return

    existing_unique_sets = _unique_index_column_sets(conn, "students")
    if any(cols == {"session_id", "normalized_roll_number"} for cols in existing_unique_sets):
        return  # already in place from a prior startup — nothing to do

    duplicates = _student_duplicate_identity_groups(conn)
    if duplicates:
        print("=" * 64)
        print("[SmartQuiz] WARNING: found pre-existing duplicate Student records")
        print("[SmartQuiz] (same live session + same roll number), created before")
        print("[SmartQuiz] the Phase 2.1A duplicate-join fix existed. Leaving them")
        print("[SmartQuiz] exactly as they are -- NOT deleting or merging anything.")
        for session_id, norm_roll, count in duplicates:
            print(f"[SmartQuiz]   session_id={session_id} roll={norm_roll!r}: {count} rows")
        print("[SmartQuiz] The database-level uniqueness index will be created")
        print("[SmartQuiz] automatically once these are resolved manually.")
        print("[SmartQuiz] New joins are already protected at the application level.")
        print("=" * 64)
        return

    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_students_session_normalized_roll
        ON students (session_id, normalized_roll_number)
        WHERE normalized_roll_number != ''
    """))


def _run_migrations() -> None:
    """Add any columns the current models expect that an older copy of a
    table doesn't have yet. Safe to run every startup — each check is a
    no-op once the column already exists. Each table is checked
    independently so an old DB missing one table doesn't skip migrating
    another."""
    with engine.connect() as conn:
        _migrate_response_session_uniqueness(conn)
        conn.commit()

        tables = _existing_tables(conn)

        if "responses" in tables:
            cols = _existing_columns(conn, "responses")

            if "quiz_id" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN quiz_id INTEGER"))

            if "is_correct" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN is_correct BOOLEAN DEFAULT 0"))

            if "submitted_at" not in cols:
                if "answered_at" in cols:
                    # column was renamed in this version — carry the old data forward
                    conn.execute(text("ALTER TABLE responses RENAME COLUMN answered_at TO submitted_at"))
                else:
                    conn.execute(text("ALTER TABLE responses ADD COLUMN submitted_at DATETIME"))

            if "score" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN score INTEGER DEFAULT 0"))

            if "response_time_ms" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN response_time_ms INTEGER"))

            if "evaluation_method" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN evaluation_method VARCHAR(20)"))
            if "evaluation_status" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN evaluation_status VARCHAR(20)"))
            if "evaluation_confidence" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN evaluation_confidence FLOAT"))
            if "evaluation_reason" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN evaluation_reason TEXT"))

            # Historical question snapshot (Phase 3, Issue 2) — see the
            # Response model docstring for why: a mutable Question row
            # must never let editing it retroactively change what a past
            # session's results display. Nullable; pre-existing rows
            # simply have no snapshot and keep falling back to the live
            # Question object, exactly as they did before this column
            # existed.
            if "question_text_snapshot" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN question_text_snapshot TEXT"))
            if "question_type_snapshot" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN question_type_snapshot VARCHAR(20)"))
            if "question_options_snapshot" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN question_options_snapshot TEXT"))
            if "correct_answer_snapshot" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN correct_answer_snapshot TEXT"))
            if "question_points_snapshot" not in cols:
                conn.execute(text("ALTER TABLE responses ADD COLUMN question_points_snapshot INTEGER"))

        if "sessions" in tables:
            session_cols = _existing_columns(conn, "sessions")
            if "current_question_index" not in session_cols:
                conn.execute(
                    text("ALTER TABLE sessions ADD COLUMN current_question_index INTEGER DEFAULT 0")
                )
            if "current_question_started_at" not in session_cols:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN current_question_started_at DATETIME"))
                # Recovery for sessions that were already mid-quiz when this
                # column was added: we deliberately do NOT backfill from the
                # session's original started_at. That timestamp could be
                # arbitrarily old, which would make the current question's
                # timer look already-expired the instant the app restarts —
                # triggering an immediate, unfair auto-lock/auto-submit for
                # every student on that question. Instead, any in-flight
                # question gets a brand-new full-length countdown starting
                # from right now, which is the safe choice: no one is
                # penalized by a fabricated deadline, and the teacher/
                # students simply see a fresh timer for the question
                # they're already on.
                conn.execute(
                    text(
                        "UPDATE sessions SET current_question_started_at = :now "
                        "WHERE current_question_index > 0 AND current_question_started_at IS NULL"
                    ),
                    {"now": datetime.now(timezone.utc).isoformat(sep=" ")},
                )

        if "quizzes" in tables:
            quiz_cols = _existing_columns(conn, "quizzes")
            if "auto_advance" not in quiz_cols:
                conn.execute(text("ALTER TABLE quizzes ADD COLUMN auto_advance BOOLEAN DEFAULT 0"))
            if "exam_mode" not in quiz_cols:
                conn.execute(text("ALTER TABLE quizzes ADD COLUMN exam_mode BOOLEAN DEFAULT 0 NOT NULL"))

        if "questions" in tables:
            question_cols = _existing_columns(conn, "questions")
            if "grading_notes" not in question_cols:
                conn.execute(text("ALTER TABLE questions ADD COLUMN grading_notes TEXT"))
            if "duration_seconds" not in question_cols:
                conn.execute(text("ALTER TABLE questions ADD COLUMN duration_seconds INTEGER DEFAULT 600"))
                # Backfill from each question's OWN quiz.duration_minutes
                # (the prior countdown source, before per-question timing
                # existed) rather than a flat default — this is what
                # "a safe default consistent with current application
                # behavior" means here: an existing quiz's timing doesn't
                # silently change the moment this migration runs. Clamped
                # to the valid 15-1200s range since a quiz could have had
                # duration_minutes set outside what's now a valid
                # per-question bound (e.g. 30 min -> clamped to 1200s).
                conn.execute(text("""
                    UPDATE questions
                    SET duration_seconds = MIN(1200, MAX(15,
                        (SELECT duration_minutes * 60 FROM quizzes WHERE quizzes.id = questions.quiz_id)
                    ))
                """))

        if "students" in tables:
            student_cols = _existing_columns(conn, "students")
            if "result_token" not in student_cols:
                conn.execute(text("ALTER TABLE students ADD COLUMN result_token VARCHAR(43)"))
                # Backfill every existing student with a token too, so a
                # student who joined before this migration doesn't lose
                # access to their own results page.
                rows = conn.execute(text("SELECT id FROM students WHERE result_token IS NULL")).fetchall()
                for (student_id,) in rows:
                    conn.execute(
                        text("UPDATE students SET result_token = :token WHERE id = :id"),
                        {"token": secrets.token_urlsafe(32), "id": student_id},
                    )
            if "current_question_index" not in student_cols:
                conn.execute(text("ALTER TABLE students ADD COLUMN current_question_index INTEGER DEFAULT 0"))
            if "question_started_at" not in student_cols:
                conn.execute(text("ALTER TABLE students ADD COLUMN question_started_at DATETIME"))

            # Phase 2.1A — duplicate-join prevention / reconnection.
            # normalized_roll_number is the IDENTITY key used to detect
            # "this is the same student re-joining the same live
            # session" (see normalize_roll_number above and join_submit
            # in app.py) — distinct from the displayed `roll_number`,
            # which is never modified by any of this.
            if "normalized_roll_number" not in student_cols:
                conn.execute(text("ALTER TABLE students ADD COLUMN normalized_roll_number VARCHAR(50)"))
            # Backfill any row that doesn't have one yet — every
            # pre-existing row the first time this column is added
            # above, and (defensively, matching the same spirit as the
            # result_token backfill) any row some other code path left
            # NULL. A cheap no-op once every row already has a value.
            stragglers = conn.execute(
                text("SELECT id, roll_number FROM students WHERE normalized_roll_number IS NULL")
            ).fetchall()
            for student_id, roll_number in stragglers:
                conn.execute(
                    text("UPDATE students SET normalized_roll_number = :norm WHERE id = :id"),
                    {"norm": normalize_roll_number(roll_number), "id": student_id},
                )
            # Database-level uniqueness backstop — see the function's
            # own docstring for the full safety story around
            # pre-existing duplicate data.
            _migrate_student_session_roll_uniqueness(conn)

        if "sessions" in tables:
            session_cols = _existing_columns(conn, "sessions")
            # Phase 2 — Classroom Radio. Every pre-existing session gets
            # the exact safe defaults the spec calls for: no track
            # selected, STOPPED, position 0 — a session created before
            # this feature existed must not suddenly acquire broken or
            # surprising radio state.
            if "radio_track_id" not in session_cols:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN radio_track_id INTEGER"))
            if "radio_status" not in session_cols:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN radio_status VARCHAR(10) DEFAULT 'STOPPED'"))
            if "radio_position_seconds" not in session_cols:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN radio_position_seconds FLOAT DEFAULT 0"))
            if "radio_changed_at" not in session_cols:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN radio_changed_at DATETIME"))
            if "radio_revision" not in session_cols:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN radio_revision INTEGER DEFAULT 0"))

        conn.commit()


def init_db() -> None:
    """Create the SQLite file and any declared tables if they don't exist
    yet, then run auto-migrations for any newer columns."""
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def get_db():
    """FastAPI dependency that yields a database session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
