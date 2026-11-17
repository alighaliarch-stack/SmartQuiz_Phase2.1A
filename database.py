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
            if "time_limit_seconds" not in quiz_cols:
                # Existing quizzes stay NULL (no new cap): published quizzes
                # and historical sessions keep behaving exactly as before.
                conn.execute(text("ALTER TABLE quizzes ADD COLUMN time_limit_seconds INTEGER"))
            if "auto_advance" not in quiz_cols:
                conn.execute(text("ALTER TABLE quizzes ADD COLUMN auto_advance BOOLEAN DEFAULT 0"))

        if "questions" in tables:
            question_cols = _existing_columns(conn, "questions")
            if "grading_notes" not in question_cols:
                conn.execute(text("ALTER TABLE questions ADD COLUMN grading_notes TEXT"))
            # Phase 3A provenance: nullable, additive, never backfilled by
            # guesswork - questions created before this stay NULL.
            if "generated_question_id" not in question_cols:
                conn.execute(text(
                    "ALTER TABLE questions ADD COLUMN generated_question_id INTEGER "
                    "REFERENCES generated_questions(id)"
                ))
            if "source_document_id" not in question_cols:
                conn.execute(text(
                    "ALTER TABLE questions ADD COLUMN source_document_id INTEGER REFERENCES documents(id)"
                ))
            if "source_document_version_id" not in question_cols:
                conn.execute(text(
                    "ALTER TABLE questions ADD COLUMN source_document_version_id INTEGER "
                    "REFERENCES document_versions(id)"
                ))
            if "source_reference" not in question_cols:
                conn.execute(text("ALTER TABLE questions ADD COLUMN source_reference VARCHAR(255)"))
            if "source_passage_id" not in question_cols:
                conn.execute(text("ALTER TABLE questions ADD COLUMN source_passage_id VARCHAR(40)"))
            # Phase 4 question-bank provenance: nullable and additive.
            if "bank_question_id" not in question_cols:
                conn.execute(text(
                    "ALTER TABLE questions ADD COLUMN bank_question_id INTEGER REFERENCES bank_questions(id)"
                ))
            if "question_bank_id" not in question_cols:
                conn.execute(text(
                    "ALTER TABLE questions ADD COLUMN question_bank_id INTEGER REFERENCES question_banks(id)"
                ))
            for column in ("generated_question_id", "source_document_id", "source_document_version_id",
                           "bank_question_id", "question_bank_id"):
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS ix_questions_{column} ON questions({column})"
                ))
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
            if "account_id" not in student_cols:
                # Nullable and additive: existing quiz-session participants
                # remain valid anonymous records with no account association.
                conn.execute(text(
                    "ALTER TABLE students ADD COLUMN account_id INTEGER "
                    "REFERENCES student_accounts(id)"
                ))

        if "student_accounts" in tables:
            account_cols = _existing_columns(conn, "student_accounts")
            if "session_version" not in account_cols:
                conn.execute(text(
                    "ALTER TABLE student_accounts ADD COLUMN session_version "
                    "INTEGER NOT NULL DEFAULT 0"
                ))
            if "failed_login_attempts" not in account_cols:
                conn.execute(text(
                    "ALTER TABLE student_accounts ADD COLUMN failed_login_attempts "
                    "INTEGER NOT NULL DEFAULT 0"
                ))
            if "locked_until" not in account_cols:
                conn.execute(text(
                    "ALTER TABLE student_accounts ADD COLUMN locked_until DATETIME"
                ))
            # Phase 4A: soft deletion / anonymisation of student accounts.
            for column in ("deleted_at", "anonymized_at"):
                if column not in account_cols:
                    conn.execute(text(f"ALTER TABLE student_accounts ADD COLUMN {column} DATETIME"))

        # Phase 4A: soft deletion of source documents ("Recently deleted").
        if "documents" in tables and "deleted_at" not in _existing_columns(conn, "documents"):
            conn.execute(text("ALTER TABLE documents ADD COLUMN deleted_at DATETIME"))

        if "learning_modules" in tables:
            module_cols = _existing_columns(conn, "learning_modules")
            if "status" not in module_cols:
                # Existing learning modules stay unavailable until a
                # teacher explicitly publishes them.
                conn.execute(text(
                    "ALTER TABLE learning_modules ADD COLUMN status "
                    "VARCHAR(20) NOT NULL DEFAULT 'DRAFT'"
                ))

        # Phase 3A source provenance. Existing versions/cards/questions keep
        # NULL here and continue to use their original source_reference label.
        if "document_versions" in tables:
            if "source_passages" not in _existing_columns(conn, "document_versions"):
                conn.execute(text("ALTER TABLE document_versions ADD COLUMN source_passages TEXT"))
        for generated_table in ("flashcards", "generated_questions"):
            if generated_table in tables and "source_passage_id" not in _existing_columns(conn, generated_table):
                conn.execute(text(f"ALTER TABLE {generated_table} ADD COLUMN source_passage_id VARCHAR(40)"))

        # Phase 3B content lifecycle and course publication.
        if "courses" in tables:
            course_cols = _existing_columns(conn, "courses")
            if "status" not in course_cols:
                # Courses that already exist were always reachable by enrolled
                # students (no course-level gate existed), so they start
                # PUBLISHED. New courses default to DRAFT in the model.
                conn.execute(text(
                    "ALTER TABLE courses ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'PUBLISHED'"
                ))
            if "archived_at" not in course_cols:
                conn.execute(text("ALTER TABLE courses ADD COLUMN archived_at DATETIME"))
            if "deleted_at" not in course_cols:
                conn.execute(text("ALTER TABLE courses ADD COLUMN deleted_at DATETIME"))
        if "learning_modules" in tables and "archived_at" not in _existing_columns(conn, "learning_modules"):
            conn.execute(text("ALTER TABLE learning_modules ADD COLUMN archived_at DATETIME"))
        for content_table in ("flashcards", "generated_questions"):
            if content_table not in tables:
                continue
            content_cols = _existing_columns(conn, content_table)
            newly_added = "course_id" not in content_cols
            if newly_added:
                conn.execute(text(
                    f"ALTER TABLE {content_table} ADD COLUMN course_id INTEGER REFERENCES courses(id)"
                ))
            if "is_published" not in content_cols:
                conn.execute(text(
                    f"ALTER TABLE {content_table} ADD COLUMN is_published BOOLEAN NOT NULL DEFAULT 0"
                ))
            for column in ("assigned_at", "published_at", "archived_at", "deleted_at"):
                if column not in content_cols:
                    conn.execute(text(f"ALTER TABLE {content_table} ADD COLUMN {column} DATETIME"))
            if "generation_request_id" not in content_cols:  # Phase 4A idempotency token
                conn.execute(text(f"ALTER TABLE {content_table} ADD COLUMN generation_request_id VARCHAR(64)"))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{content_table}_generation_request_id "
                f"ON {content_table}(generation_request_id)"
            ))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{content_table}_course_id ON {content_table}(course_id)"
            ))
            if newly_added:
                # One-time mapping of the old review states, run only in the
                # migration that introduces the lifecycle columns:
                #   APPROVED -> assigned to its module's course and published
                #               (it was student-visible before; it stays so)
                #   REJECTED -> archived (reversible; nothing is discarded)
                #   PENDING / DRAFT -> left unassigned in the content bank
                now = datetime.now(timezone.utc).isoformat(sep=" ")
                conn.execute(text(
                    f"UPDATE {content_table} SET "
                    "course_id = (SELECT course_id FROM learning_modules "
                    f"WHERE learning_modules.id = {content_table}.module_id), "
                    "is_published = 1, assigned_at = :now, published_at = :now "
                    "WHERE approval_status = 'APPROVED'"
                ), {"now": now})
                conn.execute(text(
                    f"UPDATE {content_table} SET archived_at = :now WHERE approval_status = 'REJECTED'"
                ), {"now": now})

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
