"""
app.py
------
SmartQuiz entrypoint.

Responsibilities so far:
  * start cleanly, connect to SQLite (creating the file/tables if needed)
  * serve the public homepage and join placeholder
  * full CRUD for Quiz metadata
  * Question Builder: full CRUD for questions belonging to a quiz
    (mcq_single, mcq_multiple, true_false, short_answer), including
    optional per-question images saved under /static/uploads

Students and live quiz-taking are still out of scope.

Run with:
    uvicorn app:app --reload
"""

import hmac
import io
import json
import os
import re
import secrets
import shutil
import uuid
import wave
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Must run before anything below reads GEMINI_API_KEY / ANTHROPIC_API_KEY /
# any other secret — a .env file sitting in the project root does NOT get
# picked up by os.environ on its own; something has to actually parse it
# and set the variables. This is that "something", and it runs once here
# at process startup, before ai_grading (or any route) ever executes.
# Safe to call even if .env doesn't exist (no-op) or the variable is
# already set in the real shell environment (load_dotenv does not
# override an already-set value by default, so real env vars still win).
load_dotenv()

import qrcode
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

import ai_grading
import auth
import models  # noqa: F401  (registers models onto Base before init_db runs)
from config import settings
from database import SessionLocal, get_db, init_db, normalize_roll_number
from models import MusicTrack, Question, Quiz, Student, SubmissionEvent
from models import Response as StudentResponse
from models import Session as LiveSession
from models import generate_result_token, generate_session_code

UPLOADS_DIR = settings.BASE_DIR / "static" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Classroom Radio (Phase 2) audio library — a subdirectory of the same
# controlled uploads tree question images already use, not a separate
# unmanaged location.
AUDIO_UPLOADS_DIR = UPLOADS_DIR / "audio"
AUDIO_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

QUESTION_TYPES = {
    "mcq_single": "Multiple Choice (Single)",
    "mcq_multiple": "Multiple Choice (Multiple)",
    "true_false": "True / False",
    "short_answer": "Short Answer",
}

SUCCESS_MESSAGES = {
    "created": "Quiz created successfully.",
    "updated": "Quiz updated successfully.",
    "deleted": "Quiz deleted successfully.",
    "published": "Quiz published successfully.",
    "unpublished": "Quiz moved back to draft.",
    "question_created": "Question added successfully.",
    "question_updated": "Question updated successfully.",
    "question_deleted": "Question deleted successfully.",
    "question_duplicated": "Question duplicated successfully.",
    "removed": "Student removed successfully.",
    "session_started": "Session started — students can now join.",
    "session_ended": "Session ended.",
    "quiz_started": "Quiz started — students are now on Question 1.",
    "next_question": "Advanced to the next question.",
}

STATUS_BADGE_CLASS = {
    "Draft": "badge-draft",
    "Published": "badge-published",
    "Live": "badge-live",
    "Completed": "badge-completed",
    "Archived": "badge-archived",
}

OPTION_LETTERS = ["A", "B", "C", "D"]

# Per-question timer bounds (Phase 1). 15s is short enough to be a
# genuinely "quick" question without being unusably fast to read and
# answer; 1200s (20 min) covers anything from a short written response
# to a substantial worked problem without allowing an effectively
# unbounded timer.
QUESTION_DURATION_MIN_SECONDS = 15
QUESTION_DURATION_MAX_SECONDS = 1200


def _validate_question_duration(raw_value: str) -> tuple[int | None, str | None]:
    """Returns (seconds, None) on success, or (None, error_message) on
    anything invalid — missing, non-numeric, or outside the 15-1200s
    range. Never raises; the caller decides how to surface the error
    (this app's existing pattern is a redirect with a query-string
    message, not a raw framework error page)."""
    if raw_value is None or str(raw_value).strip() == "":
        return None, "Question duration is required."
    try:
        seconds = int(str(raw_value).strip())
    except ValueError:
        return None, "Question duration must be a whole number of seconds."
    if seconds < QUESTION_DURATION_MIN_SECONDS or seconds > QUESTION_DURATION_MAX_SECONDS:
        return None, (
            f"Question duration must be between {QUESTION_DURATION_MIN_SECONDS} seconds "
            f"and {QUESTION_DURATION_MAX_SECONDS} seconds (20 minutes)."
        )
    return seconds, None


def _format_duration_human(seconds: int) -> str:
    """Human-friendly display for a duration in seconds — 'sensible and
    configurable' formatting used in the question builder/list rather
    than always showing a raw second count."""
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return f"{minutes} min"
    return f"{seconds} sec"


# ---------------------------------------------------------------------------
# Classroom Radio (Phase 2) — audio upload validation.
#
# Three layers, all stdlib-only (no new dependency):
#   1. Extension whitelist — rejects anything not claiming to be one of
#      these formats outright.
#   2. Magic-byte content sniffing — reads the first few bytes of the
#      ACTUAL uploaded content and checks it matches a real audio
#      format's own file signature, regardless of what extension or
#      Content-Type the client claimed. This is what stops a renamed
#      script/executable from being accepted just because someone named
#      it "track.mp3" — verified directly against disguised content in
#      testing (see the Phase 2 test report).
#   3. A streamed, hard size cap — reads the upload in chunks and aborts
#      (deleting any partial file) the moment the cumulative size
#      exceeds the limit, rather than trusting a Content-Length header
#      (which can be absent or wrong) or buffering an unbounded upload
#      fully before checking.
# ---------------------------------------------------------------------------

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}
MAX_AUDIO_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB — generous for a classroom track, not unbounded
AUDIO_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB per read


def _sniff_audio_format(head: bytes) -> str | None:
    """Identifies a real audio format from its own file signature (the
    actual bytes), independent of any claimed extension or
    Content-Type. Returns None if the content doesn't match any
    supported format's signature — the caller treats that as a
    rejected upload, not a best-effort guess."""
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"  # ID3v2 tag, or a raw MPEG audio frame sync pattern
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "m4a"  # ISO-BMFF/MPEG-4 container box signature
    return None


def _extract_wav_duration(filepath: Path) -> float | None:
    """Exact duration for a WAV file via Python's stdlib `wave` module
    — no new dependency. Returns None (never raises) if the file can't
    be parsed as WAV for any reason; duration is a nice-to-have display
    field, not something that should ever break an upload."""
    try:
        with wave.open(str(filepath), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate <= 0:
                return None
            return round(frames / float(rate), 2)
    except (wave.Error, EOFError, OSError):
        return None


def _validate_and_save_audio_upload(upload: UploadFile) -> tuple[str | None, float | None, int | None, str | None]:
    """Validates and saves an uploaded audio file under the controlled
    AUDIO_UPLOADS_DIR. Returns (storage_filename, duration_seconds,
    file_size_bytes, error_message) — exactly one of (storage_filename,
    error_message) is set.

    The stored filename is ALWAYS server-generated (uuid4 + the
    validated extension) — the uploader's original filename is used
    nowhere in the storage path, which is what rules out path traversal
    and filename-based tricks by construction rather than by trying to
    sanitize untrusted input after the fact.
    """
    if not upload or not upload.filename:
        return None, None, None, "No file was uploaded."

    ext = Path(upload.filename).suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_AUDIO_EXTENSIONS))
        return None, None, None, f"Unsupported file type. Allowed formats: {allowed}."

    storage_filename = f"track_{uuid.uuid4().hex}{ext}"
    dest = AUDIO_UPLOADS_DIR / storage_filename

    total_size = 0
    head_bytes = b""
    try:
        with dest.open("wb") as out:
            while True:
                chunk = upload.file.read(AUDIO_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                if not head_bytes:
                    head_bytes = chunk[:16]
                total_size += len(chunk)
                if total_size > MAX_AUDIO_FILE_SIZE_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    limit_mb = MAX_AUDIO_FILE_SIZE_BYTES // (1024 * 1024)
                    return None, None, None, f"File is too large. Maximum size is {limit_mb} MB."
                out.write(chunk)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return None, None, None, f"Could not save the uploaded file: {exc}"

    if total_size == 0:
        dest.unlink(missing_ok=True)
        return None, None, None, "The uploaded file is empty."

    sniffed = _sniff_audio_format(head_bytes)
    if sniffed is None:
        # Content doesn't match any real audio format's own signature,
        # regardless of what extension it claimed — reject rather than
        # trust the filename/Content-Type.
        dest.unlink(missing_ok=True)
        return None, None, None, "This file doesn't look like a valid audio file."

    duration = _extract_wav_duration(dest) if ext == ".wav" else None
    return storage_filename, duration, total_size, None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # create the SQLite file / tables before the app starts serving
    db = SessionLocal()
    try:
        auth.ensure_teacher_password_bootstrapped(db)
    finally:
        db.close()
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
templates = Jinja2Templates(directory=str(settings.TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(settings.BASE_DIR / "static")), name="static")


@app.exception_handler(auth.TeacherAuthRequired)
async def _teacher_auth_required_handler(request: Request, exc: auth.TeacherAuthRequired):
    return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)


@app.middleware("http")
async def _admin_auth_gate(request: Request, call_next):
    """Server-side gate for every /admin/* route — this is what actually
    enforces "students must not be able to access teacher functionality"
    (not CSS/JS hiding, which was explicitly insufficient). Runs at the
    middleware layer rather than as a per-route Depends() so a route
    added later under /admin is protected automatically, with no risk of
    forgetting to add the dependency to one of them."""
    if request.url.path.startswith("/admin") and not auth.is_teacher_authenticated(request):
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
    return await call_next(request)


# Signs the session cookie (itsdangerous — already an existing transitive
# dependency of FastAPI/Starlette, so this adds no new third-party
# package). SESSION_SECRET_KEY should be set in .env for a stable value
# across restarts; if it isn't, a random one is generated at each
# startup, which simply means any previously logged-in browser gets
# signed out on restart (safe default — the teacher just logs in again).
#
# Registered AFTER _admin_auth_gate deliberately: Starlette runs
# middleware in REVERSE registration order (the most-recently-added
# middleware is outermost, so it runs first on the way in). Since
# _admin_auth_gate needs to read request.session, SessionMiddleware
# must be the one that runs first / wraps around it — i.e. added later
# in this file. Registering it before _admin_auth_gate (as an earlier
# version of this file did) makes every request 500 with "SessionMiddleware
# must be installed to access request.session", since the auth gate
# would run before the session had been attached to the request at all.
_session_secret = os.environ.get("SESSION_SECRET_KEY") or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax")


def _get_quiz_or_404(db: Session, quiz_id: int) -> Quiz:
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    return quiz


def _get_question_or_404(db: Session, quiz_id: int, question_id: int) -> Question:
    question = (
        db.query(Question)
        .filter(Question.id == question_id, Question.quiz_id == quiz_id)
        .first()
    )
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    return question


def _save_uploaded_image(image: UploadFile | None) -> str | None:
    """Save an optional uploaded image under /static/uploads and return its
    public path, or None if no file was provided."""
    if not image or not image.filename:
        return None
    ext = Path(image.filename).suffix or ".png"
    fname = f"q_{uuid.uuid4().hex[:10]}{ext}"
    dest = UPLOADS_DIR / fname
    with dest.open("wb") as f:
        shutil.copyfileobj(image.file, f)
    return f"/static/uploads/{fname}"


def _build_question_payload(
    question_type: str,
    option_a: str, option_b: str, option_c: str, option_d: str,
    correct_single: str,
    option_a_correct: bool, option_b_correct: bool, option_c_correct: bool, option_d_correct: bool,
    correct_true_false: str,
    accepted_answers: str,
) -> tuple[list, object]:
    """Translate the raw question form fields into (options_list, correct_answer)
    ready to be JSON-encoded, based on the selected question type."""
    letter_values = {"A": option_a, "B": option_b, "C": option_c, "D": option_d}

    if question_type == "mcq_single":
        options_list = [v.strip() for v in letter_values.values() if v.strip()]
        chosen = letter_values.get(correct_single, "").strip()
        correct_value = chosen if chosen in options_list else None

    elif question_type == "mcq_multiple":
        flags = {
            "A": option_a_correct, "B": option_b_correct,
            "C": option_c_correct, "D": option_d_correct,
        }
        options_list = [v.strip() for v in letter_values.values() if v.strip()]
        correct_value = [
            letter_values[letter].strip()
            for letter in OPTION_LETTERS
            if flags[letter] and letter_values[letter].strip()
        ]

    elif question_type == "true_false":
        options_list = ["True", "False"]
        correct_value = correct_true_false if correct_true_false in options_list else "True"

    elif question_type == "short_answer":
        options_list = []
        # One accepted answer per LINE, not comma-separated — a single
        # correct answer very commonly contains its own commas as normal
        # sentence punctuation (e.g. "identify strengths, weaknesses,
        # opportunities and threats"), and splitting on commas silently
        # shredded such an answer into unmatchable fragments. This was
        # the actual root cause of Short Answer always grading Incorrect.
        correct_value = [line.strip() for line in accepted_answers.splitlines() if line.strip()]

    else:
        options_list, correct_value = [], None

    return options_list, correct_value


def _question_prefill(question: Question) -> dict:
    """Decode a stored question's JSON columns into flat fields the
    question form template can drop straight into inputs/checkboxes."""
    options = json.loads(question.options or "[]")
    correct = json.loads(question.correct_answer or "null")
    letters = OPTION_LETTERS

    prefill = {
        "option_a": options[0] if len(options) > 0 else "",
        "option_b": options[1] if len(options) > 1 else "",
        "option_c": options[2] if len(options) > 2 else "",
        "option_d": options[3] if len(options) > 3 else "",
        "correct_single": "",
        "correct_flags": {"A": False, "B": False, "C": False, "D": False},
        "correct_true_false": "True",
        "accepted_answers": "",
        "grading_notes": question.grading_notes or "",
        "duration_seconds": question.duration_seconds,
    }

    if question.question_type == "mcq_single" and isinstance(correct, str):
        for letter, opt in zip(letters, options):
            if opt == correct:
                prefill["correct_single"] = letter
    elif question.question_type == "mcq_multiple" and isinstance(correct, list):
        for letter, opt in zip(letters, options):
            prefill["correct_flags"][letter] = opt in correct
    elif question.question_type == "true_false" and isinstance(correct, str):
        prefill["correct_true_false"] = correct
    elif question.question_type == "short_answer" and isinstance(correct, list):
        prefill["accepted_answers"] = "\n".join(correct)

    return prefill


def _compute_response_time_ms(question_started_at: datetime | None, submitted_at: datetime) -> int | None:
    """response_time_ms = submitted_at - question_started_at, where
    question_started_at is session.current_question_started_at — the
    SAME authoritative, server-only anchor the shared countdown timer
    is built from (see _question_ends_at). Deliberately NOT derived
    from any client-supplied timestamp: an earlier version of this
    function used a per-student "page displayed at" value round-tripped
    through a hidden form field, which is both less accurate (skewed by
    page-load latency) and not tamper-resistant (a client could echo
    back an arbitrary value). Using the server's own timer anchor fixes
    both, and is exactly what "the existing authoritative question
    timing mechanism" refers to.

    If there's no active timer for some reason, response time is simply
    not recorded rather than guessed at."""
    if not question_started_at:
        return None
    if question_started_at.tzinfo is None:
        # Same naive-but-actually-UTC situation _question_ends_at already
        # handles for this exact column — SQLite doesn't persist tzinfo,
        # so a value read back from the DB needs it reattached explicitly
        # rather than being compared against an aware datetime blindly.
        question_started_at = question_started_at.replace(tzinfo=timezone.utc)
    delta_ms = int((submitted_at - question_started_at).total_seconds() * 1000)
    return max(delta_ms, 0)  # clock skew safety net — never negative


def _check_answer_correct(
    question: Question, selected_option: str, selected_options: list[str] | None = None
) -> bool:
    """Compare a submitted answer against the question's stored correct
    answer, branching by question_type — this is the single place that
    understands every type's storage shape (set by _build_question_payload
    in the teacher's question form):

      mcq_single / true_false  correct_answer: a single string
      mcq_multiple             correct_answer: a list of strings
      short_answer              correct_answer: a list of accepted strings

    mcq_multiple compares the selected set against the correct set with
    Python's set equality, so selection order never matters. short_answer
    compares case-insensitively after trimming both sides.
    """
    correct = json.loads(question.correct_answer or "null")
    qtype = question.question_type

    if qtype in ("mcq_single", "true_false"):
        if not selected_option or not isinstance(correct, str):
            return False
        return selected_option == correct

    if qtype == "mcq_multiple":
        chosen = set(selected_options or [])
        if not chosen or not isinstance(correct, list):
            return False
        return chosen == set(correct)

    if qtype == "short_answer":
        if not selected_option or not isinstance(correct, list):
            return False
        given = selected_option.strip().lower()
        accepted = {str(a).strip().lower() for a in correct}
        return given != "" and given in accepted

    return False


def _score_mcq_multiple(question: Question, selected_options: list[str]) -> tuple[float, bool]:
    """Proportional scoring for Multiple Choice (Multiple) — the
    authoritative policy (not an invented alternative):

      1. awarded = (correct options selected / total correct options)
                   * question.points, IF no incorrect option was selected.
      2. Any incorrect option selected at all -> awarded = 0, regardless
         of how many correct options were also selected.
      3. Selecting the complete correct set -> full points.
      4. Selecting nothing -> 0.

    Returns (awarded_points, is_fully_correct). is_fully_correct is True
    only when the selected set exactly equals the correct set (full
    marks) — a partial-credit response is NOT "fully correct", which is
    what "number of fully correct answers" means as a leaderboard
    tie-breaker (see _compute_results).

    Verified against every example in the spec:
      3 correct answers, 100 pts: 3/3->100, 2/3->66.67, 1/3->33.33,
      0->0, any wrong selected->0 in every case.
    """
    correct = json.loads(question.correct_answer or "null")
    if not isinstance(correct, list) or not correct:
        return 0.0, False

    correct_set = set(correct)
    chosen_set = set(selected_options or [])

    if not chosen_set:
        return 0.0, False

    if chosen_set - correct_set:
        # At least one incorrect option was selected — the whole
        # response is worth 0, no matter how many correct ones were
        # also picked. This is the rule, not "correct minus wrong".
        return 0.0, False

    ratio = len(chosen_set & correct_set) / len(correct_set)
    awarded = round(ratio * question.points, 2)
    is_fully_correct = chosen_set == correct_set
    return awarded, is_fully_correct


def _normalize_answer_text(text: str) -> str:
    """Normalize for Level 1 exact-match comparison: trim, lowercase,
    collapse repeated whitespace, and drop trailing sentence punctuation
    (a period/question mark/etc at the very end doesn't change whether
    two answers mean the same thing) — but keep punctuation that's part
    of a word, like the apostrophe in "organization's", since stripping
    that could change meaning."""
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[.!?,;:]+$", "", text).strip()
    return text


# ---------------------------------------------------------------------------
# Level 2 — synonym-aware, required-vs-optional concept coverage matching.
#
# This is deliberately NOT a literal string search. Two things make it
# tolerant of paraphrasing the way Level 1 (exact match) can't be:
#
#   1. SYNONYM_GROUPS canonicalizes common equivalent wordings (type/kind/
#      form, AI/artificial intelligence, creates/makes/generates, ...)
#      before any comparison happens, so a synonym substitution doesn't
#      register as a "missing" word.
#   2. The teacher's expected answer is split into REQUIRED concept
#      phrases (the definitional core) and OPTIONAL examples (anything
#      introduced by "such as" / "like" / "for example" / "including") —
#      see _split_core_and_examples. A student who covers every required
#      concept but skips some examples still passes; a student who lists
#      examples but misses a required concept does not.
#
# Coverage per required concept is a RATIO, not a strict subset, and
# common stopwords (a, the, is, that, based, ...) are excluded before
# comparing — this is what lets a student restate the subject with a
# pronoun ("It is...") instead of repeating the term verbatim, satisfying
# "do not require the student's answer to contain every word of the
# model answer." The threshold is deliberately high (see
# LEVEL2_CONCEPT_COVERAGE_THRESHOLD) so it only fires on strong, clear
# matches; anything softer falls through to AI rather than risking a
# false positive — Level 2 only ever resolves to CORRECT or "keep going",
# never to INCORRECT, matching the hybrid pipeline's design.
#
# Known limitation (documented, not hidden): this is still bag-of-words
# under the hood, so it is grammar-blind. A pathological answer that
# scatters every required content word without forming a coherent
# sentence could in principle satisfy the ratio check. The threshold is
# kept high specifically to make that hard (nearly every content word of
# EVERY required concept must appear), and Level 3 (AI) is the real
# semantic backstop for exactly this failure mode — see TEST 9 in the
# test report.
# ---------------------------------------------------------------------------

SYNONYM_GROUPS: list[list[str]] = [
    ["type", "kind", "form", "category"],
    ["artificial intelligence", "ai"],
    ["creates", "create", "makes", "make", "generates", "generate", "produces", "produce", "generative"],
    ["allows", "allow", "lets", "let", "enables", "enable", "permits", "permit"],
    ["uses", "use", "utilizes", "utilize", "employs", "employ"],
    ["requires", "require", "needs", "need"],
    ["shows", "show", "displays", "display", "demonstrates", "demonstrate"],
    ["big", "large", "huge", "massive"],
    ["small", "tiny", "little", "minor"],
    ["fast", "quick", "rapid", "speedy"],
    ["begin", "start", "commence"],
    ["end", "finish", "conclude", "complete"],
    ["important", "significant", "essential", "critical", "key"],
    ["help", "assist", "aid"],
]

# Words with no semantic content for coverage purposes — excluded so a
# concept phrase's required words are only the ones that actually carry
# meaning. Kept as a plain, auditable list rather than an external NLP
# stopword library, matching "keep the implementation explainable".
STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "that", "which", "who", "this", "these", "those", "in",
    "on", "at", "for", "and", "or", "but", "its", "it", "from", "as",
    "by", "with", "based", "such", "like", "including", "other", "into",
    "than", "then", "there", "their", "not",
})

# How much of a required concept's content words must appear (after
# synonym canonicalization and stopword removal) for Level 2 to treat
# that concept as satisfied. Named and centralized here — per "the exact
# thresholds should be sensible and configurable rather than hard-coded
# blindly" — rather than a magic number buried in a comparison.
LEVEL2_CONCEPT_COVERAGE_THRESHOLD = 0.75

# Below this AI confidence, a CORRECT/INCORRECT decision is downgraded to
# NEEDS_REVIEW rather than trusted — the AI is not allowed to award (or
# deny) points on a guess. Same "named constant, not a buried literal"
# reasoning as above.
AI_HIGH_CONFIDENCE_THRESHOLD = 0.80


def _canonicalize_synonyms(text: str) -> str:
    """Replace known synonym/equivalent phrasings with one canonical
    form (the first entry in each SYNONYM_GROUPS list) before any
    comparison — this is what makes coverage checking synonym-aware
    instead of a literal string search. Longest variants are replaced
    first so a multi-word synonym doesn't get partially clobbered by a
    shorter one nested inside it."""
    text = text.lower()
    for group in SYNONYM_GROUPS:
        canonical = group[0]
        for variant in sorted(group[1:], key=len, reverse=True):
            text = re.sub(r"\b" + re.escape(variant) + r"\b", canonical, text)
    return text


def _content_words(text: str) -> set[str]:
    """Synonym-canonicalized, punctuation-stripped, stopword-filtered
    word set — the unit Level 2's coverage ratio is computed over."""
    text = _canonicalize_synonyms(text)
    text = re.sub(r"[^\w\s]", " ", text)
    return {w for w in text.split() if w and w not in STOPWORDS}


# A clause containing one of these must not be allowed to contribute
# words toward "this concept is present" — otherwise a sentence that
# NEGATES a required concept ("...does not use patterns learned from
# data") would still register as covering it, since bag-of-words
# matching has no grammar awareness on its own. Clause boundaries here
# are kept coarse (sentence-ish: '.', ';', 'and', 'but', ',') on
# purpose: dropping a whole clause rather than trying to scope the
# negation to just part of it only ever pushes MORE cases to Level 3
# (AI) than strictly necessary — never fewer — which keeps this from
# ever being a source of a false positive.
_NEGATION_MARKERS = frozenset({
    "not", "n't", "never", "without", "no", "none", "nothing", "lacks", "lacking", "cannot",
})
_NEGATION_PHRASES = (
    "rather than", "instead of", "fails to", "fail to", "does not", "did not",
    "do not", "is not", "are not", "was not", "were not", "can not", "no longer",
)


def _strip_negated_clauses(text: str) -> str:
    """Removes any clause containing a negation marker/phrase before
    Level 2 computes coverage — see the rationale above _NEGATION_MARKERS."""
    clauses = re.split(r"[.;]|\band\b|\bbut\b|,", text, flags=re.IGNORECASE)
    kept = []
    for clause in clauses:
        low = " " + clause.lower().replace("'", "") + " "
        has_negation = any(f" {m} " in low for m in _NEGATION_MARKERS)
        has_negation = has_negation or any(phrase in low for phrase in _NEGATION_PHRASES)
        if not has_negation:
            kept.append(clause)
    return " ".join(kept)


def _keyword_bag(text: str) -> set[str]:
    """Word-level bag with NO stopword filtering — used only for the
    legacy short, explicit teacher-authored alternate-answer lines
    (see _grade_short_answer's second accepted-answer-line check),
    where every word in a short deliberately-written line is assumed
    meaningful, unlike an auto-derived concept phrase."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return {word for word in text.split() if word}


_EXAMPLES_TRIGGER = r"(?:such as|for example|e\.g\.,?|including|like)"
_EXAMPLES_PATTERN = re.compile(
    r"(?P<opendash>[\u2014\u2013(]\s*)?" + _EXAMPLES_TRIGGER +
    r"\s+(?P<examples>.+?)(?=\s*[\u2014\u2013)]|\s*,?\s*\bbased on\b|[.;]|$)",
    re.IGNORECASE,
)


def _split_core_and_examples(text: str) -> tuple[str, list[str]]:
    """Splits a model answer into its definitional CORE (required) and
    an optional EXAMPLES list, by detecting a clause introduced by
    "such as" / "like" / "for example" / "including" (optionally set off
    by em/en-dashes or parentheses, e.g. "...content—such as text,
    images—based on..."). If no such clause exists, the whole text is
    the core and there are no examples. This is what lets Level 2 (and
    the AI prompt) distinguish "the student didn't mention every
    example" (fine) from "the student is missing a required concept"
    (not fine)."""
    m = _EXAMPLES_PATTERN.search(text)
    if not m:
        return text, []
    examples_blob = m.group("examples")
    examples = [e.strip(" .") for e in re.split(r",|\band\b|\bor\b", examples_blob) if e.strip(" .")]
    start = m.start("opendash") if m.group("opendash") else m.start()
    end = m.end()
    rest = text[end:]
    dash_m = re.match(r"\s*[\u2014\u2013]\s*", rest)
    if dash_m:
        end += dash_m.end()
    core = (text[:start] + " " + text[end:]).strip()
    core = re.sub(r"\s+", " ", core)
    return core, examples


def _extract_required_concepts(core_text: str) -> list[str]:
    """Coarse clause splitting of the CORE (post-examples-removal) text
    into required-concept phrases. Deliberately coarse — a handful of
    broad chunks, not a chunk per word — because over-fragmenting would
    make Level 2 reject valid paraphrases that restructure the sentence
    (sending them to AI instead, which is always safe, just less
    efficient than resolving at Level 2). If splitting yields nothing
    usable (e.g. a one-word expected answer like "Paris"), the whole
    core text is used as a single required concept rather than being
    discarded."""
    text = core_text.strip().rstrip(".")
    if not text:
        return []
    parts = re.split(r"\bthat\b|\bwhich\b|\bbased on\b|,\s*(?:and|which|that)\b", text, flags=re.IGNORECASE)
    concepts = [p.strip() for p in parts if p.strip()]
    concepts = [c for c in concepts if len(_content_words(c)) >= 1]
    return concepts if concepts else [text]


def _concept_coverage_ratio(concept_phrase: str, answer_text: str) -> float:
    """Fraction of a required concept's content words (post-synonym-
    canonicalization, post-stopword-removal) that appear somewhere in
    the student's answer OUTSIDE of a negated clause. 1.0 for a concept
    with no content words at all (nothing to require)."""
    concept_words = _content_words(concept_phrase)
    if not concept_words:
        return 1.0
    safe_answer_text = _strip_negated_clauses(answer_text)
    answer_words = _content_words(safe_answer_text)
    return len(concept_words & answer_words) / len(concept_words)


def _grade_short_answer(question: Question, student_answer: str) -> dict:
    """The full Level 1 -> 2 -> 3 pipeline for Short Answer grading
    (see the module-level docstring in ai_grading.py for Level 3).
    Returns a dict with is_correct plus the evaluation_* fields stored
    on the Response row:

        is_correct              bool — drives scoring, same as every
                                 other question type
        evaluation_method        "exact" | "keyword" | "ai" | None
        evaluation_status        "CORRECT" | "INCORRECT" | "NEEDS_REVIEW"
        evaluation_confidence    float 0..1, only set for "ai"
        evaluation_reason        short explanation, only set for "ai"
                                 or when AI evaluation was unavailable
        needs_ai                 True if Level 1/2 couldn't classify
                                 this and Level 3 (a background AI call)
                                 is required — the caller inserts a
                                 provisional NEEDS_REVIEW/EVALUATING
                                 response and schedules the AI call
                                 rather than blocking on it here.

    Levels 1 and 2 are pure string/set operations and always resolve
    synchronously — no AI call, no network, no latency. Level 2 only
    ever resolves to CORRECT or "needs more evaluation" — it never
    marks an answer INCORRECT on its own (see the module docstring
    above), so a false Level-2 rejection just costs one AI call, never
    an unfair grade.
    """
    accepted_raw = json.loads(question.correct_answer or "null")
    accepted = [str(a) for a in accepted_raw] if isinstance(accepted_raw, list) else []

    given_raw = (student_answer or "").strip()
    if not given_raw:
        return {
            "is_correct": False, "evaluation_method": None,
            "evaluation_status": "INCORRECT", "evaluation_confidence": None,
            "evaluation_reason": None, "needs_ai": False,
        }

    given_norm = _normalize_answer_text(given_raw)

    # LEVEL 1 — normalized exact match against any accepted answer.
    for candidate in accepted:
        if _normalize_answer_text(candidate) == given_norm:
            return {
                "is_correct": True, "evaluation_method": "exact",
                "evaluation_status": "CORRECT", "evaluation_confidence": None,
                "evaluation_reason": None, "needs_ai": False,
            }

    # LEVEL 2a — synonym-aware required-concept coverage against the
    # PRIMARY accepted answer (accepted[0], the teacher's full model
    # answer). Every required concept (core text minus any "such as..."
    # examples clause) must clear the coverage threshold.
    if accepted:
        core, _examples = _split_core_and_examples(accepted[0])
        required_concepts = _extract_required_concepts(core)
        if required_concepts:
            ratios = [_concept_coverage_ratio(c, given_raw) for c in required_concepts]
            if all(r >= LEVEL2_CONCEPT_COVERAGE_THRESHOLD for r in ratios):
                return {
                    "is_correct": True, "evaluation_method": "keyword",
                    "evaluation_status": "CORRECT", "evaluation_confidence": None,
                    "evaluation_reason": None, "needs_ai": False,
                }

    # LEVEL 2b — legacy path, preserved for backward compatibility: any
    # additional accepted-answer line that's SHORT (a deliberate teacher-
    # authored keyword list, not a full sentence) still requires exact
    # containment of every one of its words, unmodified from before this
    # change. Existing questions authored this way keep working exactly
    # as they did.
    given_words = _keyword_bag(given_raw)
    for candidate in accepted:
        candidate_words = _keyword_bag(candidate)
        if candidate_words and len(candidate_words) <= 6 and candidate_words.issubset(given_words):
            return {
                "is_correct": True, "evaluation_method": "keyword",
                "evaluation_status": "CORRECT", "evaluation_confidence": None,
                "evaluation_reason": None, "needs_ai": False,
            }

    # LEVEL 3 — needs semantic (AI) evaluation. The caller is responsible
    # for not blocking the student on this — see the background task.
    return {
        "is_correct": False, "evaluation_method": None,
        "evaluation_status": "EVALUATING", "evaluation_confidence": None,
        "evaluation_reason": None, "needs_ai": True,
    }


def _run_ai_grading_task(response_id: int, question_id: int, student_answer: str) -> None:
    """Runs AFTER the HTTP response has already been sent to the student
    (via FastAPI BackgroundTasks) — this is what keeps AI evaluation from
    blocking the live quiz. Opens its own database session, since the
    request-scoped one from Depends(get_db) is already closed by the
    time this runs.

    Per the AI-safety spec: below AI_HIGH_CONFIDENCE_THRESHOLD, a
    CORRECT/INCORRECT decision is downgraded to NEEDS_REVIEW rather than
    trusted — the AI is not allowed to award (or deny) points on a
    guess. Any failure (no provider configured, network error, malformed
    response) also lands on NEEDS_REVIEW with the reason preserved,
    never silently becomes an ordinary Incorrect, and never crashes the
    request that scheduled it (this function runs outside the request
    lifecycle entirely by the time any of this executes).

    The stored evaluation_reason folds in the AI's matched/missing
    concepts and any contradiction it flagged (when the provider
    returned them) so a teacher reviewing Results gets a concrete,
    auditable explanation rather than just a bare decision — e.g. "Core
    concepts matched: type of AI, creates new content. Missing: patterns
    from data." No separate DB columns needed for this: it's one more
    sentence folded into the existing evaluation_reason text field.

    evaluation_method is set to the actual provider used (e.g. "gemini")
    rather than a generic "ai" label, so Results can show a teacher
    whether a decision came from deterministic matching, a specific AI
    provider, or a manual override — see AI_GRADING_PROVIDER.
    """
    db = SessionLocal()
    try:
        response = db.query(StudentResponse).filter(StudentResponse.id == response_id).first()
        question = db.query(Question).filter(Question.id == question_id).first()
        if not response or not question:
            return  # response or question was deleted in the meantime — nothing to update

        accepted_raw = json.loads(question.correct_answer or "null")
        accepted = [str(a) for a in accepted_raw] if isinstance(accepted_raw, list) else []
        expected_primary = accepted[0] if accepted else ""
        alternatives = accepted[1:] if len(accepted) > 1 else []
        provider_name = os.environ.get("AI_GRADING_PROVIDER", "gemini")

        try:
            result = ai_grading.evaluate_semantic_match(
                question_text=question.question_text,
                expected_answer=expected_primary,
                accepted_answers=alternatives,
                student_answer=student_answer,
                points=question.points,
                grading_notes=question.grading_notes or "",
            )
            decision = result["decision"]
            confidence = result["confidence"]
            reason = _compose_ai_reason(result)
            if confidence < AI_HIGH_CONFIDENCE_THRESHOLD and decision != "NEEDS_REVIEW":
                decision = "NEEDS_REVIEW"
        except ai_grading.AIEvaluationUnavailable as exc:
            decision, confidence, reason = "NEEDS_REVIEW", None, f"AI evaluation unavailable: {exc}"

        response.is_correct = decision == "CORRECT"
        response.score = question.points if decision == "CORRECT" else 0
        response.evaluation_method = provider_name
        response.evaluation_status = decision
        response.evaluation_confidence = confidence
        response.evaluation_reason = reason
        db.commit()
    finally:
        db.close()


def _compose_ai_reason(result: dict) -> str:
    """Builds the concise, teacher-facing explanation shown on Results
    (Part 5's "Core concepts matched; wording differs from the model
    answer." style summary) from the AI's structured response. Falls
    back to the AI's own free-text reason if the richer optional fields
    (matched_concepts / missing_optional_concepts / contradictions)
    weren't provided by the current provider — never assumes they're
    present, so this stays compatible with any provider that only
    returns the required decision/confidence/reason shape."""
    reason = str(result.get("reason") or "").strip()
    matched = result.get("matched_concepts") or []
    missing = result.get("missing_optional_concepts") or []
    contradictions = result.get("contradictions") or []

    parts = []
    if reason:
        parts.append(reason)
    if matched:
        parts.append("Matched: " + ", ".join(matched[:4]) + ".")
    if missing:
        parts.append("Missing: " + ", ".join(missing[:4]) + ".")
    if contradictions:
        parts.append("Contradicts: " + ", ".join(contradictions[:2]) + ".")
    return " ".join(parts) if parts else reason


def _to_utc_iso_z(dt: datetime) -> str:
    """Serialize a datetime as an explicit UTC ISO 8601 string ending in
    'Z' — the ONLY unambiguous format for a browser's Date() constructor.

    Root cause this exists to fix: every datetime in this app is WRITTEN
    using datetime.now(timezone.utc) (correctly UTC-aware), but
    SQLAlchemy's SQLite DateTime column type does not persist tzinfo —
    values read back from the database come back as naive datetimes
    (tzinfo=None), even though they are still actually UTC instants.
    If a naive datetime's .isoformat() is sent straight to the browser
    (e.g. "2026-08-10T11:20:12.468920", no "Z", no offset), the
    ECMAScript Date Time String Format spec requires JS to parse a
    date-TIME string with no timezone designator as LOCAL browser time,
    not UTC. On any browser whose local timezone isn't UTC, this makes
    `new Date(...)` resolve to a different instant than the server
    intended — often several hours off — which reads as "already
    expired" the moment the page loads. That was the actual bug behind
    both the "timer shows 0 immediately" and "auto-advance fires almost
    instantly" reports: every downstream symptom traced back to this
    one missing timezone marker.

    This function is the single place that closes the gap: naive values
    are explicitly treated as UTC (never as local time), and already-
    aware values are normalized to UTC, before formatting with a
    trailing 'Z' that leaves no room for the browser to guess wrong.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _current_question(session: LiveSession) -> Question | None:
    """The question the session is currently showing, or None if it
    isn't on a real question right now (WAITING before Start Quiz, or
    QUIZ_COMPLETED after the last one). `session.quiz.questions` is
    already ordered by Question.id via the relationship in models.py,
    which is the same order question_number/current_question_index
    have always referred to elsewhere in this app."""
    questions = session.quiz.questions
    idx = session.current_question_index
    if idx < 1 or idx > len(questions):
        return None
    return questions[idx - 1]


def _question_ends_at(session: LiveSession) -> str | None:
    """ISO-8601 UTC timestamp (explicit 'Z') for when the CURRENT
    question's countdown expires, or None if there's no active question
    clock yet. Both the student player and the teacher's Live Sessions
    card compute their countdown from this same value, so everyone sees
    the same timer for a given question regardless of when they
    personally loaded the page — and regardless of their browser's local
    timezone, since the value is unambiguous UTC.

    The countdown LENGTH comes from the current question's own
    `duration_seconds` — each question has its own independent timer
    (Phase 1). This deliberately no longer reads session.quiz.
    duration_minutes at all; that field is preserved for its other
    existing uses (quiz summary display) but is not consulted here."""
    if not session.current_question_started_at:
        return None
    question = _current_question(session)
    if not question:
        return None
    started_at = session.current_question_started_at
    if started_at.tzinfo is None:
        # See _to_utc_iso_z: this naive value is always actually UTC,
        # written via datetime.now(timezone.utc) — SQLite just doesn't
        # remember that. Attach it explicitly rather than guessing.
        started_at = started_at.replace(tzinfo=timezone.utc)
    ends_at = started_at + timedelta(seconds=question.duration_seconds)
    return _to_utc_iso_z(ends_at)


# ---------------------------------------------------------------------------
# Per-student question progression (Auto Advance mode only).
#
# When quiz.auto_advance is False, every function below returns exactly
# what the existing global session fields already say — this mode is
# the original, fully synchronized, teacher-controlled flow and is
# provably untouched by anything in this section.
#
# When quiz.auto_advance is True, each student paces themselves:
# Student.current_question_index/question_started_at (advanced the
# instant THEY submit) drive their own position. Classroom Mode retains
# its existing client timeout submission; Exam Mode advances past an
# expired unanswered question without creating a Response.
# The teacher's existing Next Question / Complete Quiz controls remain
# a class-wide "catch everyone up" override: a student who's already
# ahead of the global index is never pulled backward (max() only ever moves a
# student's effective position forward), but a student still lagging
# behind can be pushed forward by the teacher if needed.
# ---------------------------------------------------------------------------

def _student_effective_question_index(quiz: Quiz, session: LiveSession, student: Student) -> int:
    """The question number THIS student should be shown right now."""
    if not quiz.auto_advance:
        return session.current_question_index
    return max(student.current_question_index, session.current_question_index)


def _student_effective_question_started_at(quiz: Quiz, session: LiveSession, student: Student) -> datetime | None:
    """The timer anchor for whichever question _student_effective_question_index
    says this student is on — their own personal anchor while their own
    pace is what's driving their position, or the session's shared
    anchor on the rare occasion a teacher's manual override has caught
    up to or passed them (that action is what actually moved them, so
    its anchor is the correct one to time from)."""
    if not quiz.auto_advance:
        return session.current_question_started_at
    if student.current_question_index >= session.current_question_index:
        return student.question_started_at
    return session.current_question_started_at


def _student_question_ends_at(quiz: Quiz, session: LiveSession, student: Student) -> str | None:
    """Per-student equivalent of _question_ends_at — identical math,
    applied to whichever (index, anchor) pair actually governs this
    particular student's current question."""
    deadline = _student_question_deadline(quiz, session, student)
    return _to_utc_iso_z(deadline) if deadline else None


def _student_question_deadline(
    quiz: Quiz, session: LiveSession, student: Student
) -> datetime | None:
    index = _student_effective_question_index(quiz, session, student)
    started_at = _student_effective_question_started_at(quiz, session, student)
    if not started_at or index < 1 or index > len(quiz.questions):
        return None
    return _utc_datetime(started_at) + timedelta(
        seconds=quiz.questions[index - 1].duration_seconds
    )


def _ensure_student_question_state(session: LiveSession, student: Student) -> bool:
    """Lazily begins this student's own personal pacing the first time
    they need it — auto_advance is on, the quiz has actually started
    (Start Quiz already clicked — session.current_question_index >= 1),
    and this student hasn't begun their own progression yet, whether
    because they were already in the waiting room when Start Quiz was
    clicked or they're joining after it already started. Either way
    their personal journey begins now, at Question 1, with a full-length
    timer. Returns True if it changed anything (so the caller knows to
    commit); never commits itself, since this is called from routes
    that may have other changes to save in the same transaction."""
    quiz = session.quiz
    if not quiz.auto_advance:
        return False
    if student.current_question_index != 0:
        return False
    if session.current_question_index < 1:
        return False
    student.current_question_index = 1
    student.question_started_at = datetime.now(timezone.utc)
    return True


# ---------------------------------------------------------------------------
# Classroom Radio (Phase 2) — server-authoritative playback state.
#
# Deliberately a SEPARATE system from the quiz timer functions above:
# nothing here reads current_question_index or current_question_started_at,
# and nothing above reads any radio_* field. A session's radio state is
# read/written entirely independently of quiz progression, by design —
# pausing/advancing/finishing the quiz must never touch it.
#
# Same synchronization model as the quiz timer, for the same reason (a
# single server-side anchor every client computes its own position FROM,
# rather than the server pushing continuous updates): the CURRENT
# position is never stored continuously — only a (position, changed_at)
# anchor plus a status. While PLAYING, position = radio_position_seconds
# + elapsed time since radio_changed_at. While PAUSED or STOPPED, the
# anchor position is the current position outright (no elapsed time
# added — nothing is advancing).
# ---------------------------------------------------------------------------

def _radio_effective_state(session: LiveSession) -> dict:
    """Computes the CURRENT authoritative radio state for `session` —
    the single function every read (student/teacher polling) AND every
    state-changing action (which needs to know 'where are we right now'
    before applying PAUSE/SEEK/etc) goes through, so the position math
    is identical everywhere and never duplicated.

    Handles end-of-track detection lazily: if the track's duration is
    known (currently: WAV only — see _extract_wav_duration) and PLAYING
    would have advanced past it, this reports STOPPED at the track's
    end. It does not require a background job to "notice" this — the
    next read or the next teacher action naturally sees and persists it.
    Tracks with unknown duration (non-WAV, best-effort per the library's
    own scope) simply never trigger this — they play until explicitly
    stopped, which is an honest limitation, not a silent wrong answer.
    """
    track = session.radio_track
    duration = track.duration_seconds if track else None

    if session.radio_status == "PLAYING" and session.radio_changed_at:
        changed_at = session.radio_changed_at
        if changed_at.tzinfo is None:
            # Same naive-but-actually-UTC situation the quiz timer
            # functions handle for this exact DateTime column type —
            # SQLite doesn't persist tzinfo on read-back.
            changed_at = changed_at.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - changed_at).total_seconds()
        position = session.radio_position_seconds + max(elapsed, 0.0)
        status = "PLAYING"
    else:
        position = session.radio_position_seconds
        status = session.radio_status

    if duration is not None and position >= duration:
        position = duration
        status = "STOPPED"

    return {
        "status": status,
        "position": round(max(position, 0.0), 2),
        "track_id": track.id if track else None,
        "track_name": track.display_name if track else None,
        "track_url": f"/static/uploads/audio/{track.storage_filename}" if track else None,
        "duration": duration,
        "revision": session.radio_revision,
        "server_time": _to_utc_iso_z(datetime.now(timezone.utc)),
    }


def _radio_freeze(session: LiveSession) -> dict:
    """Writes the CURRENT effective position/status back into the
    session row as a fresh anchor — 'stop the clock right here'. Every
    state-changing action calls this first, then applies its own change
    on top (PAUSE keeps the frozen position and sets status=PAUSED; SEEK
    adjusts the frozen position by a delta; RESET/STOP override it
    outright; etc). This also picks up any lazy end-of-track transition
    before the new action is applied, so e.g. pressing Seek right after
    a track naturally ended doesn't seek relative to a stale position.

    Does NOT commit and does NOT touch radio_revision — the caller
    applies its own change and increments the revision once, as a
    single atomic action.
    """
    effective = _radio_effective_state(session)
    session.radio_position_seconds = effective["position"]
    session.radio_status = effective["status"]
    session.radio_changed_at = datetime.now(timezone.utc)
    return effective


def _student_completed_quiz(
    db: Session, student_id: int, quiz_id: int, session_id: int | None = None
) -> bool:
    """The ONLY place that decides whether an individual student has
    finished a quiz ATTEMPT: they've submitted a response to every
    question in it. This is deliberately independent of the session's
    shared current_question_index — a session being on (or past) its
    last question says nothing about whether any particular student has
    actually answered everything. Every route that might send a student
    to /completed must go through this, not through session/question
    index comparisons.

    session_id further scopes this to one specific attempt when given
    (every join already creates a fresh Student row tied to its
    session, so student_id alone already disambiguates attempts today —
    but passing session_id when it's available makes that scoping
    explicit by construction rather than relying on that incidental
    behavior)."""
    total_questions = db.query(Question).filter(Question.quiz_id == quiz_id).count()
    if total_questions == 0:
        return False
    query = db.query(StudentResponse.question_id).filter(
        StudentResponse.student_id == student_id, StudentResponse.quiz_id == quiz_id
    )
    if session_id is not None:
        query = query.filter(StudentResponse.session_id == session_id)
    answered_questions = query.distinct().count()
    return answered_questions >= total_questions


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _exam_submission_result(
    db: Session,
    *,
    code: str,
    received_at: datetime,
    session: LiveSession,
    student_id: int,
    question_id: int,
    question_number: int,
    authoritative_question: Question | None,
    deadline: datetime | None,
    response_id: int | None = None,
    success: bool = False,
    already_submitted: bool = False,
    student_completed: bool = False,
    next_question_number: int | None = None,
) -> dict:
    event = SubmissionEvent(
        server_timestamp=datetime.now(timezone.utc),
        server_received_at=received_at,
        session_id=session.id,
        student_id=student_id,
        quiz_id=session.quiz_id,
        submitted_question_id=question_id,
        submitted_question_number=question_number,
        event_type=code,
        authoritative_question_id=authoritative_question.id if authoritative_question else None,
        deadline=deadline,
        response_id=response_id,
    )
    db.add(event)
    db.commit()
    return {
        "success": success,
        "code": code,
        "event_id": event.id,
        "already_submitted": already_submitted,
        "student_completed": student_completed,
        "next_question_number": next_question_number,
        "response_id": response_id,
    }


def _submit_exam_mode_answer(
    db: Session,
    background_tasks: BackgroundTasks,
    *,
    session_id: int,
    student_id: int,
    question_id: int,
    question_number: int,
    selected_option: str,
    selected_options: list[str],
    received_at: datetime,
) -> dict:
    _lock_session_for_submission(db, session_id)
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    student = db.query(Student).filter(Student.id == student_id).first()
    question = db.query(Question).filter(Question.id == question_id).first()
    if not student or student.session_id != session.id or student.quiz_id != session.quiz_id:
        current = _current_question(session)
        started_at = session.current_question_started_at
        deadline = (
            _utc_datetime(started_at) + timedelta(seconds=current.duration_seconds)
            if current and started_at else None
        )
        return _exam_submission_result(
            db, code="SUBMISSION_STUDENT_NOT_IN_SESSION", received_at=received_at,
            session=session, student_id=student_id, question_id=question_id,
            question_number=question_number, authoritative_question=current,
            deadline=deadline,
        )

    quiz = session.quiz
    questions = quiz.questions
    current_index = _student_effective_question_index(quiz, session, student)
    authoritative_question = (
        questions[current_index - 1] if 1 <= current_index <= len(questions) else None
    )
    started_at = (
        _student_effective_question_started_at(quiz, session, student)
        if authoritative_question else None
    )
    deadline = (
        _utc_datetime(started_at) + timedelta(seconds=authoritative_question.duration_seconds)
        if started_at and authoritative_question else None
    )

    if session.status != "Live":
        return _exam_submission_result(
            db, code="SUBMISSION_SESSION_ENDED", received_at=received_at,
            session=session, student_id=student.id, question_id=question_id,
            question_number=question_number,
            authoritative_question=authoritative_question, deadline=deadline,
        )

    if not question or question.quiz_id != session.quiz_id:
        return _exam_submission_result(
            db, code="SUBMISSION_WRONG_SESSION_QUESTION", received_at=received_at,
            session=session, student_id=student_id, question_id=question_id,
            question_number=question_number,
            authoritative_question=authoritative_question, deadline=deadline,
        )

    existing = (
        db.query(StudentResponse)
        .filter(
            StudentResponse.student_id == student.id,
            StudentResponse.session_id == session.id,
            StudentResponse.question_id == question.id,
        )
        .first()
    )
    if existing:
        return _exam_submission_result(
            db, code="SUBMISSION_ALREADY_SUBMITTED", received_at=received_at,
            session=session, student_id=student.id, question_id=question.id,
            question_number=question_number,
            authoritative_question=authoritative_question,
            deadline=deadline, response_id=existing.id, success=True,
            already_submitted=True,
            student_completed=_student_completed_quiz(
                db, student.id, session.quiz_id, session.id
            ),
        )

    if _student_completed_quiz(db, student.id, session.quiz_id, session.id):
        return _exam_submission_result(
            db, code="SUBMISSION_STUDENT_COMPLETED", received_at=received_at,
            session=session, student_id=student.id, question_id=question.id,
            question_number=question_number,
            authoritative_question=authoritative_question, deadline=deadline,
        )

    submission_started_at = started_at
    submission_deadline = deadline
    timely_auto_advance_race = False
    if (
        quiz.auto_advance
        and 1 <= question_number <= len(questions)
        and questions[question_number - 1].id == question.id
    ):
        expiry_event = (
            db.query(SubmissionEvent)
            .filter(
                SubmissionEvent.session_id == session.id,
                SubmissionEvent.student_id == student.id,
                SubmissionEvent.submitted_question_id == question.id,
                SubmissionEvent.event_type == "QUESTION_EXPIRED_UNANSWERED",
            )
            .order_by(SubmissionEvent.server_timestamp.desc())
            .first()
        )
        expired_deadline = _utc_datetime(expiry_event.deadline) if expiry_event and expiry_event.deadline else None
        if expired_deadline and _utc_datetime(received_at) > expired_deadline:
            return _exam_submission_result(
                db, code="SUBMISSION_EXPIRED", received_at=received_at,
                session=session, student_id=student.id, question_id=question.id,
                question_number=question_number,
                authoritative_question=authoritative_question,
                deadline=expired_deadline,
            )
        if (
            expired_deadline
            and student.current_question_index >= session.current_question_index
        ):
            submission_started_at = expired_deadline - timedelta(seconds=question.duration_seconds)
            if _utc_datetime(received_at) < submission_started_at:
                return _exam_submission_result(
                    db, code="SUBMISSION_NOT_CURRENT", received_at=received_at,
                    session=session, student_id=student.id,
                    question_id=question.id, question_number=question_number,
                    authoritative_question=authoritative_question,
                    deadline=expired_deadline,
                )
            submission_deadline = expired_deadline
            timely_auto_advance_race = True

    if (
        not authoritative_question
        or authoritative_question.id != question.id
        or question_number != current_index
    ) and not timely_auto_advance_race:
        return _exam_submission_result(
            db, code="SUBMISSION_NOT_CURRENT", received_at=received_at,
            session=session, student_id=student.id, question_id=question.id,
            question_number=question_number,
            authoritative_question=authoritative_question, deadline=deadline,
        )

    if not submission_started_at:
        return _exam_submission_result(
            db, code="SUBMISSION_NOT_CURRENT", received_at=received_at,
            session=session, student_id=student.id, question_id=question.id,
            question_number=question_number,
            authoritative_question=authoritative_question, deadline=deadline,
        )

    if _utc_datetime(received_at) > submission_deadline:
        return _exam_submission_result(
            db, code="SUBMISSION_EXPIRED", received_at=received_at,
            session=session, student_id=student.id, question_id=question.id,
            question_number=question_number,
            authoritative_question=authoritative_question, deadline=deadline,
        )

    if question.question_type == "mcq_multiple":
        stored_answer = json.dumps(sorted(selected_options)) if selected_options else None
    else:
        stripped = selected_option.strip() if selected_option else ""
        stored_answer = stripped or None

    if question.question_type == "short_answer":
        grading = _grade_short_answer(question, stored_answer or "")
        is_correct = grading["is_correct"]
        score = question.points if is_correct else 0
        evaluation_method = grading["evaluation_method"]
        evaluation_status = grading["evaluation_status"]
        evaluation_confidence = grading["evaluation_confidence"]
        evaluation_reason = grading["evaluation_reason"]
        needs_ai = grading["needs_ai"]
    elif question.question_type == "mcq_multiple":
        score, is_correct = _score_mcq_multiple(question, selected_options)
        evaluation_method = None
        evaluation_status = "CORRECT" if is_correct else "INCORRECT"
        evaluation_confidence = None
        evaluation_reason = None
        needs_ai = False
    else:
        is_correct = _check_answer_correct(question, selected_option, selected_options)
        score = question.points if is_correct else 0
        evaluation_method = None
        evaluation_status = "CORRECT" if is_correct else "INCORRECT"
        evaluation_confidence = None
        evaluation_reason = None
        needs_ai = False

    response = StudentResponse(
        student_id=student.id,
        session_id=session.id,
        quiz_id=quiz.id,
        question_id=question.id,
        selected_option=stored_answer,
        is_correct=is_correct,
        score=score,
        response_time_ms=_compute_response_time_ms(submission_started_at, received_at),
        submitted_at=received_at,
        evaluation_method=evaluation_method,
        evaluation_status=evaluation_status,
        evaluation_confidence=evaluation_confidence,
        evaluation_reason=evaluation_reason,
        question_text_snapshot=question.question_text,
        question_type_snapshot=question.question_type,
        question_options_snapshot=question.options,
        correct_answer_snapshot=question.correct_answer,
        question_points_snapshot=question.points,
    )
    db.add(response)
    db.flush()

    next_question_number = None
    if quiz.auto_advance:
        if not timely_auto_advance_race:
            student.current_question_index = current_index + 1
            student.question_started_at = (
                received_at if student.current_question_index <= len(questions) else None
            )
        if student.current_question_index <= len(questions):
            next_question_number = student.current_question_index

    event = SubmissionEvent(
        server_timestamp=datetime.now(timezone.utc),
        server_received_at=received_at,
        session_id=session.id,
        student_id=student.id,
        quiz_id=quiz.id,
        submitted_question_id=question.id,
        submitted_question_number=question_number,
        event_type="SUBMISSION_ACCEPTED",
        authoritative_question_id=authoritative_question.id,
        deadline=submission_deadline,
        response_id=response.id,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(StudentResponse)
            .filter(
                StudentResponse.student_id == student_id,
                StudentResponse.session_id == session_id,
                StudentResponse.question_id == question_id,
            )
            .first()
        )
        if not existing:
            raise
        return _exam_submission_result(
            db, code="SUBMISSION_ALREADY_SUBMITTED", received_at=received_at,
            session=session, student_id=student_id, question_id=question_id,
            question_number=question_number,
            authoritative_question=authoritative_question, deadline=deadline,
            response_id=existing.id, success=True, already_submitted=True,
            student_completed=_student_completed_quiz(
                db, student_id, quiz.id, session.id
            ),
        )

    student_completed = _student_completed_quiz(db, student.id, quiz.id, session.id)
    if needs_ai:
        background_tasks.add_task(
            _run_ai_grading_task, response.id, question.id, stored_answer or ""
        )
    return {
        "success": True,
        "code": "SUBMISSION_ACCEPTED",
        "event_id": event.id,
        "response_id": response.id,
        "message": "Answer submitted successfully.",
        "student_completed": student_completed,
        "next_question_number": next_question_number,
    }


def _has_active_exam_session(db: Session, quiz_id: int) -> bool:
    return (
        db.query(LiveSession.id)
        .join(Quiz, Quiz.id == LiveSession.quiz_id)
        .filter(
            LiveSession.quiz_id == quiz_id,
            LiveSession.status == "Live",
            Quiz.exam_mode.is_(True),
        )
        .first()
        is not None
    )


def _ensure_exam_questions_mutable(db: Session, quiz_id: int) -> None:
    if _has_active_exam_session(db, quiz_id):
        raise HTTPException(
            status_code=409,
            detail="Exam questions cannot be changed while an Exam Mode session is active.",
        )


def _lock_session_for_submission(db: Session, session_id: int) -> None:
    """Acquire SQLite's write reservation before reading Exam Mode state.

    Teacher progression actions acquire the same reservation first, so
    SQLite transaction order determines whether a submission or an
    advance/completion is authoritative.
    """
    db.execute(
        text("UPDATE sessions SET id = id WHERE id = :session_id"),
        {"session_id": session_id},
    )


def _advance_expired_exam_student(
    db: Session, session: LiveSession, student: Student, received_at: datetime
) -> tuple[LiveSession, Student]:
    quiz = session.quiz
    deadline = _student_question_deadline(quiz, session, student)
    index = _student_effective_question_index(quiz, session, student)
    if (
        not quiz.exam_mode
        or not quiz.auto_advance
        or not deadline
        or index >= len(quiz.questions)
        or datetime.now(timezone.utc) <= deadline
    ):
        return session, student

    session_id, student_id = session.id, student.id
    db.rollback()
    _lock_session_for_submission(db, session_id)
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    student = db.query(Student).filter(Student.id == student_id).first()
    if not session or not student:
        raise HTTPException(status_code=404, detail="Session or student not found")
    if (
        session.status != "Live"
        or student.session_id != session.id
        or not session.quiz.exam_mode
        or not session.quiz.auto_advance
        or _student_completed_quiz(db, student.id, session.quiz_id, session.id)
    ):
        return session, student

    now = datetime.now(timezone.utc)
    changed = False
    while True:
        index = _student_effective_question_index(session.quiz, session, student)
        deadline = _student_question_deadline(session.quiz, session, student)
        if not deadline or now <= deadline or index >= len(session.quiz.questions):
            break
        expired_question = session.quiz.questions[index - 1]
        db.add(
            SubmissionEvent(
                server_timestamp=now,
                server_received_at=received_at,
                session_id=session.id,
                student_id=student.id,
                quiz_id=session.quiz_id,
                submitted_question_id=expired_question.id,
                submitted_question_number=index,
                event_type="QUESTION_EXPIRED_UNANSWERED",
                authoritative_question_id=expired_question.id,
                deadline=deadline,
                response_id=None,
            )
        )
        student.current_question_index = index + 1
        student.question_started_at = deadline
        changed = True
    if changed:
        db.commit()
        db.refresh(session)
        db.refresh(student)
    return session, student


def _lock_live_session_for_quiz_action(db: Session, quiz_id: int) -> None:
    db.execute(
        text(
            "UPDATE sessions SET id = id "
            "WHERE quiz_id = :quiz_id AND status = 'Live'"
        ),
        {"quiz_id": quiz_id},
    )


def _get_active_quiz(db: Session) -> Quiz | None:
    """The quiz students can currently join without a code — the most
    recently started Live quiz. Only one is expected to be Live at a
    time in typical use, but if more than one is, the newest wins."""
    return (
        db.query(Quiz)
        .filter(Quiz.status == "Live")
        .order_by(Quiz.created_at.desc())
        .first()
    )


def _get_current_session(db: Session, quiz_id: int) -> LiveSession | None:
    """The Session row currently keeping a quiz Live, if any."""
    return (
        db.query(LiveSession)
        .filter(LiveSession.quiz_id == quiz_id, LiveSession.status == "Live")
        .order_by(LiveSession.id.desc())
        .first()
    )


def _unique_session_code(db: Session) -> str:
    """Generate a 6-character session code, retrying on the rare collision."""
    for _ in range(20):
        code = generate_session_code()
        exists = db.query(LiveSession).filter(LiveSession.session_code == code).first()
        if not exists:
            return code
    raise HTTPException(status_code=500, detail="Could not generate a unique session code")


def _session_phase(quiz: Quiz, session: LiveSession | None) -> str:
    """Names exactly where a quiz sits in the lifecycle:

        DRAFT -> PUBLISHED -> WAITING -> QUESTION_1..N
              -> QUIZ_COMPLETED -> SESSION_ENDED

    "Start Session" only ever produces WAITING (students can join and
    wait; current_question_index stays 0, no timer). Only an explicit
    "Start Quiz" click can move a session into QUESTION_1, and only
    "Next Question" can move it further — nothing else touches
    current_question_index.

    QUIZ_COMPLETED is the state after the last question is done: the
    session is still Live (nothing about it has been destroyed), there's
    just no question left to show. Reaching it is *not* the same as
    ending the session — that only happens when the teacher explicitly
    clicks "Finish Session", which is the only thing that can move a
    quiz into SESSION_ENDED. This split is what keeps a single-question
    quiz (or auto-advance running off the end of the last question) from
    instantly terminating the whole session.
    """
    if quiz.status == "Draft":
        return "DRAFT"
    if quiz.status == "Published":
        return "PUBLISHED"
    if quiz.status == "Live":
        if not session or session.current_question_index == 0:
            return "WAITING"
        total_questions = len(quiz.questions)
        if session.current_question_index <= total_questions:
            return f"QUESTION_{session.current_question_index}"
        return "QUIZ_COMPLETED"
    return "SESSION_ENDED"  # Completed / Archived


# ------------------------------------------------------------------ auth --

@app.get("/login")
def login_page(request: Request, next: str = "/admin", error: str | None = None):
    if auth.is_teacher_authenticated(request):
        return RedirectResponse(url=next or "/admin", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"app_name": settings.APP_NAME, "next": next, "error": error},
    )


@app.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/admin"),
    db: Session = Depends(get_db),
):
    stored_hash = auth.get_password_hash(db)
    if stored_hash and auth.verify_password(password, stored_hash):
        auth.log_in_teacher(request)
        # Only ever redirect within this app — an unvalidated `next` value
        # could otherwise be used to bounce a login through an external
        # URL (open-redirect). Anything not starting with a single "/"
        # (and not "//", which browsers treat as protocol-relative) falls
        # back to the dashboard.
        safe_next = next if next.startswith("/") and not next.startswith("//") else "/admin"
        return RedirectResponse(url=safe_next, status_code=303)
    return RedirectResponse(
        url=f"/login?next={next}&error=Incorrect+password.", status_code=303
    )


@app.post("/logout")
def logout_submit(request: Request):
    auth.log_out_teacher(request)
    return RedirectResponse(url="/login", status_code=303)


# ------------------------------------------------------------- public pages --

@app.get("/")
def homepage(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_name": settings.APP_NAME,
            "message": "SmartQuiz is running.",
        },
    )


@app.get("/join")
def join_page(request: Request, db: Session = Depends(get_db)):
    """Student landing page: join form for the currently published quiz."""
    return templates.TemplateResponse(
        request=request,
        name="join.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "join",
            "active_quiz": _get_active_quiz(db),
        },
    )


@app.get("/student")
def student_page(request: Request, db: Session = Depends(get_db)):
    """Alias of /join — same landing page, reachable from either URL."""
    return templates.TemplateResponse(
        request=request,
        name="join.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            "active_quiz": _get_active_quiz(db),
        },
    )


@app.get("/join/{session_code}")
def join_by_code(session_code: str, request: Request, db: Session = Depends(get_db)):
    """Join a specific live session directly by its 6-character code
    (this is the URL encoded in the QR code on the Live Sessions page)."""
    session = (
        db.query(LiveSession)
        .filter(LiveSession.session_code == session_code.upper(), LiveSession.status == "Live")
        .first()
    )
    active_quiz = session.quiz if session else None
    return templates.TemplateResponse(
        request=request,
        name="join.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            "active_quiz": active_quiz,
            "invalid_code": session_code if not session else None,
        },
    )


def _find_existing_attempt(db: Session, session_id: int | None, normalized_roll: str) -> Student | None:
    """The single lookup behind Phase 2.1A's identity rule: within ONE
    live session, `(session_id, normalized_roll_number)` identifies one
    student ATTEMPT (see normalize_roll_number in database.py and the
    matching UNIQUE index created there). Name is deliberately not part
    of this — two different students can share a name, so this must
    never match on name alone, and a blank/unnormalizable roll number
    is treated as having no identity to match against (never merges
    students by an empty string)."""
    if not session_id or not normalized_roll:
        return None
    return (
        db.query(Student)
        .filter(Student.session_id == session_id, Student.normalized_roll_number == normalized_roll)
        .first()
    )


@app.post("/join")
def join_submit(
    quiz_id: int = Form(...),
    name: str = Form(...),
    roll_number: str = Form(...),
    course: str = Form(""),
    section: str = Form(""),
    db: Session = Depends(get_db),
):
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id, Quiz.status == "Live").first()
    if not quiz:
        # the session ended between page load and submit — send them back
        return RedirectResponse(url="/join", status_code=303)

    current_session = _get_current_session(db, quiz.id)
    normalized_roll = normalize_roll_number(roll_number)

    # Reconnect, don't duplicate (Phase 2.1A): within one live session,
    # roll numbers are unique per the classroom's own rules, so a
    # second join with the same session + roll number — a resubmit, a
    # page refresh that lands back on the join form, a reconnect after
    # a dropped connection, or simply opening the join link again — is
    # the SAME student attempting to rejoin, not a new attempt. Reuse
    # the existing Student row untouched: don't overwrite name/course/
    # section (the newly submitted values are discarded in favor of the
    # original — see PHASE_2_1A's own guidance on not letting a
    # reconnect silently rewrite historical identity), don't touch
    # current_question_index/question_started_at (Auto Advance
    # progress), don't touch result_token, don't touch responses. Just
    # send them back to their own existing attempt.
    existing_student = _find_existing_attempt(db, current_session.id if current_session else None, normalized_roll)
    if existing_student:
        return RedirectResponse(url=f"/student/waiting/{existing_student.id}", status_code=303)

    student = Student(
        quiz_id=quiz.id,
        session_id=current_session.id if current_session else None,
        name=name.strip(),
        roll_number=roll_number.strip(),
        normalized_roll_number=normalized_roll,
        course=course.strip(),
        section=section.strip(),
        connected_at=datetime.now(timezone.utc),
        status="Ready",
        result_token=generate_result_token(),
    )
    db.add(student)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race: another near-simultaneous join for the SAME
        # session + roll number committed first, tripping the
        # UNIQUE(session_id, normalized_roll_number) index (see
        # database.py). Two requests can both pass the _find_existing_
        # attempt check above before either has actually committed —
        # this is the real backstop, exactly mirroring how
        # session_question_submit already handles the identical race
        # for duplicate answer submissions. Roll back this failed
        # insert, re-query the row that won the race, and reconnect to
        # IT instead — never surface a raw 500 mid-join, and never end
        # up with two attempts or a half-committed transaction.
        db.rollback()
        winner = _find_existing_attempt(db, current_session.id if current_session else None, normalized_roll)
        if winner:
            return RedirectResponse(url=f"/student/waiting/{winner.id}", status_code=303)
        # Only reachable if the IntegrityError came from something
        # other than this specific race (e.g. the unique index doesn't
        # exist yet because pre-existing duplicate data blocked its
        # creation — see _migrate_student_session_roll_uniqueness) —
        # don't silently swallow a genuine, unexpected error.
        raise
    db.refresh(student)
    return RedirectResponse(url=f"/student/waiting/{student.id}", status_code=303)


@app.get("/student/waiting/{student_id}")
def waiting_room(student_id: int, request: Request, db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    quiz = student.quiz

    if student.session and _student_completed_quiz(db, student.id, quiz.id):
        return RedirectResponse(
            url=f"/student/session/{student.session.session_code}/completed", status_code=303
        )

    if student.session_id:
        connected_count = (
            db.query(Student)
            .filter(Student.session_id == student.session_id, Student.status != "Disconnected")
            .count()
        )
    else:
        connected_count = (
            db.query(Student)
            .filter(Student.quiz_id == quiz.id, Student.status != "Disconnected")
            .count()
        )
    return templates.TemplateResponse(
        request=request,
        name="waiting_room.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            "student": student,
            "quiz": quiz,
            "connected_count": connected_count,
            "session_code": student.session.session_code if student.session else None,
        },
    )


@app.get("/session/{session_code}/status")
def session_status(session_code: str, student_id: int | None = None, db: Session = Depends(get_db)):
    """Polled every 2s by every student-facing page during a live
    session (waiting room, question player, post-answer waiting page).
    Plain JSON, no HTML.

    session_status is the SINGLE authoritative field for where the
    session sits — it comes straight from _session_phase (the same
    function that drives the teacher's Live Sessions page), collapsed
    to four values a student client needs to branch on:

        WAITING        current_question_index == 0
        IN_PROGRESS    1 <= current_question_index <= total_questions
        QUIZ_COMPLETED current_question_index > total_questions,
                       session still Live — NOT the same as SESSION_ENDED
        SESSION_ENDED  session.status == "Completed"

    Callers must branch on session_status, never on a numeric
    comparison against current_question/total_questions — that
    numeric-comparison approach is exactly what silently swallowed the
    QUIZ_COMPLETED case before and left students stuck waiting forever.

    student_completed is a SEPARATE, per-student fact (only computed
    when a student_id is passed): whether THIS student has personally
    answered every question. It has no relationship to session_status —
    a student can be student_completed=true while session_status is
    still IN_PROGRESS (they finished early), or student_completed=false
    while session_status is QUIZ_COMPLETED (they didn't finish in time).
    """
    received_at = datetime.now(timezone.utc)
    session = (
        db.query(LiveSession)
        .filter(LiveSession.session_code == session_code.upper())
        .first()
    )
    if not session:
        return {
            "status": "ENDED", "session_status": "SESSION_ENDED",
            "quiz_started": False, "current_question_index": 0, "current_question": 0,
            "total_questions": 0, "student_completed": False,
        }

    total_questions = len(session.quiz.questions)
    phase = _session_phase(session.quiz, session)
    session_status_value = "IN_PROGRESS" if phase.startswith("QUESTION_") else phase
    ended = session_status_value == "SESSION_ENDED"
    quiz_started = session_status_value == "IN_PROGRESS"
    student_completed = (
        _student_completed_quiz(db, student_id, session.quiz_id) if student_id else False
    )
    # This student's OWN effective question number — identical to
    # current_question_index when auto_advance is off (or no student_id
    # was given), but can be FURTHER ALONG than the global index when
    # auto_advance is on and this student has personally submitted
    # ahead of it. This is what the client-side polling scripts
    # navigate on; current_question_index is kept as-is for any older
    # caller that still reads it directly.
    student_current_question = session.current_question_index
    student = None
    if student_id:
        student = db.query(Student).filter(Student.id == student_id).first()
        if student and student.quiz_id == session.quiz_id:
            if _ensure_student_question_state(session, student):
                db.commit()
            if session.quiz.exam_mode and session.quiz.auto_advance:
                session, student = _advance_expired_exam_student(
                    db, session, student, received_at
                )
            student_current_question = _student_effective_question_index(session.quiz, session, student)
        else:
            student = None
    student_deadline = (
        _student_question_deadline(session.quiz, session, student)
        if student else None
    )
    exam_question_expired = bool(
        session.quiz.exam_mode
        and student_deadline
        and datetime.now(timezone.utc) > student_deadline
    )
    return {
        # Legacy field names — still read by older callers; kept for
        # backward compatibility, not used for new branching logic.
        "status": "ENDED" if ended else "LIVE",
        "current_question_index": session.current_question_index,
        "total_questions": total_questions,
        "student_completed": student_completed,
        # Authoritative fields:
        "session_status": session_status_value,
        "quiz_started": quiz_started,
        "current_question": session.current_question_index,
        "question_ends_at": _question_ends_at(session) if quiz_started else None,
        # New, additive fields for per-student progression (Auto
        # Advance) — every field above this line is completely
        # unchanged from before and still describes the GLOBAL session
        # state exactly as it always has.
        "student_current_question": student_current_question if quiz_started else session.current_question_index,
        "student_question_ends_at": (
            _student_question_ends_at(session.quiz, session, student)
            if quiz_started and student
            else None
        ),
        "exam_question_expired": exam_question_expired,
        "server_time": _to_utc_iso_z(datetime.now(timezone.utc)),
    }


@app.get("/session/{session_code}/radio/status")
def radio_status(session_code: str, db: Session = Depends(get_db)):
    """Classroom Radio's polling endpoint — read-only by construction:
    this is a plain GET with no side effects at all, so there is no
    route here a student could ever use to control playback even if
    they discovered its URL. Every actual control (play/pause/stop/
    reset/seek/track-select) lives exclusively under /admin, protected
    by the existing teacher auth middleware — this endpoint cannot
    reach any of that code.

    Returns the same _radio_effective_state shape every internal caller
    uses, so the student browser computes its playback position from
    the identical server-authoritative math the teacher's own controls
    are built on — never trusting the student's own wall clock.

    If no session or no track is selected, returns a harmless
    "nothing to play" shape rather than an error — a student's radio
    player should simply stay silent/hidden in that case, not surface
    a broken-looking failure.
    """
    session = (
        db.query(LiveSession)
        .filter(LiveSession.session_code == session_code.upper())
        .first()
    )
    if not session or not session.radio_track_id:
        return {
            "status": "STOPPED", "position": 0.0, "track_id": None, "track_name": None,
            "track_url": None, "duration": None, "revision": 0,
            "server_time": _to_utc_iso_z(datetime.now(timezone.utc)),
        }
    return _radio_effective_state(session)


@app.get("/student/session/{session_code}/question/{question_number}")
def session_question_page(
    session_code: str,
    question_number: int,
    request: Request,
    student_id: int | None = None,
    db: Session = Depends(get_db),
):
    """The real quiz player for question number `question_number` (1-indexed).
    Renders the appropriate interactive input for whichever of the four
    question types this question is (mcq_single, mcq_multiple,
    true_false, short_answer) — see session_question.html. Works for
    any question in the quiz, not just the first — this is what makes
    "unlimited questions per quiz" possible."""
    session = (
        db.query(LiveSession)
        .filter(LiveSession.session_code == session_code.upper(), LiveSession.status == "Live")
        .first()
    )
    if not session:
        return RedirectResponse(url=f"/student/session/{session_code}/ended", status_code=303)

    student = db.query(Student).filter(Student.id == student_id).first() if student_id else None
    if not student or student.quiz_id != session.quiz_id:
        return RedirectResponse(url="/join", status_code=303)

    if _student_completed_quiz(db, student.id, session.quiz_id):
        return RedirectResponse(url=f"/student/session/{session_code}/completed", status_code=303)

    if _ensure_student_question_state(session, student):
        db.commit()

    questions = (
        db.query(Question)
        .filter(Question.quiz_id == session.quiz_id)
        .order_by(Question.id)
        .all()
    )
    total_questions = len(questions)
    question = questions[question_number - 1] if 1 <= question_number <= total_questions else None
    options = json.loads(question.options or "[]") if question else []

    existing_response = None
    previous_selected_options: list[str] = []
    if question:
        existing_response = (
            db.query(StudentResponse)
            .filter(
                StudentResponse.student_id == student.id,
                StudentResponse.session_id == session.id,
                StudentResponse.question_id == question.id,
            )
            .first()
        )
        if existing_response and question.question_type == "mcq_multiple" and existing_response.selected_option:
            try:
                previous_selected_options = json.loads(existing_response.selected_option)
            except (json.JSONDecodeError, TypeError):
                previous_selected_options = []

    return templates.TemplateResponse(
        request=request,
        name="session_question.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            "quiz": session.quiz,
            "question": question,
            "question_number": question_number,
            "total_questions": total_questions,
            "options": options,
            "student": student,
            "session_code": session_code.upper(),
            "question_type_labels": QUESTION_TYPES,
            "already_submitted": existing_response is not None,
            "previous_selected_option": existing_response.selected_option if existing_response else None,
            "previous_selected_options": previous_selected_options,
            "question_ends_at": _student_question_ends_at(session.quiz, session, student),
            "question_displayed_at": datetime.now(timezone.utc).isoformat(),
        },
    )


@app.post("/student/session/{session_code}/question/{question_number}")
def session_question_submit(
    session_code: str,
    question_number: int,
    background_tasks: BackgroundTasks,
    student_id: int = Form(...),
    question_id: int = Form(...),
    selected_option: str = Form(""),
    selected_options: list[str] = Form([]),
    question_displayed_at: str = Form(""),
    db: Session = Depends(get_db),
):
    """Called via fetch() from the quiz player (see session_question.html)
    — returns JSON, not a redirect, so the page can disable itself and
    show a success message in place without navigating away.

    selected_option carries the answer for mcq_single / true_false /
    short_answer (all single-value types); selected_options carries the
    checked set for mcq_multiple (FormData naturally collects repeated
    checkbox values under one key, and FastAPI's list[str] = Form([])
    collects them here — no client-side JSON encoding needed).

    question_displayed_at is still accepted (harmless, backward
    compatible with the existing template) but is no longer what
    response time is computed from — see _compute_response_time_ms."""
    received_at = datetime.now(timezone.utc)
    session = (
        db.query(LiveSession)
        .filter(LiveSession.session_code == session_code.upper())
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session, student, or question not found")

    if session.quiz.exam_mode:
        session_id = session.id
        db.rollback()
        return _submit_exam_mode_answer(
            db,
            background_tasks,
            session_id=session_id,
            student_id=student_id,
            question_id=question_id,
            question_number=question_number,
            selected_option=selected_option,
            selected_options=selected_options,
            received_at=received_at,
        )

    student = db.query(Student).filter(Student.id == student_id).first()
    question = db.query(Question).filter(Question.id == question_id).first()
    if not student or not question:
        raise HTTPException(status_code=404, detail="Session, student, or question not found")

    # Defensive: don't assume a prior GET request (the question page, or
    # a status poll) already lazily started this student's own Auto
    # Advance pacing — a submission arriving as this student's very
    # first server contact must still be able to advance them correctly
    # afterward, not silently no-op because current_question_index was
    # still 0.
    if _ensure_student_question_state(session, student):
        db.commit()

    # Prevent duplicate submissions WITHIN this attempt: if this student
    # already answered this question in THIS session, don't insert again
    # — just confirm what's already saved. Scoped to session_id so the
    # same student answering the same quiz in a different live session
    # is a fully independent attempt, never blocked by an earlier one.
    existing = (
        db.query(StudentResponse)
        .filter(
            StudentResponse.student_id == student.id,
            StudentResponse.session_id == session.id,
            StudentResponse.question_id == question.id,
        )
        .first()
    )
    if existing:
        return {
            "success": True,
            "already_submitted": True,
            "message": "Answer submitted successfully.",
            "student_completed": _student_completed_quiz(db, student.id, question.quiz_id, session.id),
        }

    submitted_at = datetime.now(timezone.utc)
    # The authoritative timer anchor — the same value the shared
    # countdown/auto-advance is built from — not any client-supplied
    # timestamp. See _compute_response_time_ms.
    #
    # CRITICAL: only trust session.current_question_started_at when the
    # question being answered is STILL the session's current question.
    # If a student's submission arrives late (network lag, a backgrounded
    # tab catching up after reconnecting) AFTER the teacher has already
    # advanced past this question, current_question_started_at now
    # belongs to a DIFFERENT question entirely — using it would produce
    # a nonsensical response time (often wildly large, or even negative
    # before being clamped to 0). There's no way to reconstruct the true
    # elapsed time for an old question once the session has moved on (no
    # per-question start-time history is kept), so the honest answer is
    # no recorded time at all, not a fabricated one. The answer itself is
    # still accepted and graded normally either way — only the timing
    # measurement is affected.
    current_question = _current_question(session)
    if current_question and current_question.id == question.id:
        response_time_ms = _compute_response_time_ms(session.current_question_started_at, submitted_at)
    else:
        response_time_ms = None

    # Store the answer in whatever shape matches the question type. This
    # reuses the single existing `selected_option` Text column for every
    # type rather than adding new columns/tables — mcq_multiple's set is
    # JSON-encoded (sorted, so the stored text is deterministic); every
    # other type stores its raw submitted text directly, preserving the
    # student's original answer (short_answer is trimmed but not
    # lowercased, so the original casing is kept for results/history).
    if question.question_type == "mcq_multiple":
        stored_answer = json.dumps(sorted(selected_options)) if selected_options else None
    else:
        stripped = selected_option.strip() if selected_option else ""
        stored_answer = stripped or None

    if question.question_type == "short_answer":
        # Levels 1 (exact) and 2 (keyword) are pure string/set ops and
        # always resolve synchronously — no AI, no latency, no change
        # in behavior from every other question type's submission speed.
        grading = _grade_short_answer(question, stored_answer or "")
        is_correct = grading["is_correct"]
        score = question.points if is_correct else 0
        evaluation_method = grading["evaluation_method"]
        evaluation_status = grading["evaluation_status"]
        evaluation_confidence = grading["evaluation_confidence"]
        evaluation_reason = grading["evaluation_reason"]
        needs_ai = grading["needs_ai"]
    elif question.question_type == "mcq_multiple":
        # Proportional credit with the "any wrong answer zeroes the
        # response" rule — see _score_mcq_multiple's docstring for the
        # exact policy. is_correct here means FULL marks specifically,
        # matching how "fully correct" is used for leaderboard tie-
        # breaking, not "score > 0".
        score, is_correct = _score_mcq_multiple(question, selected_options)
        evaluation_method = None
        evaluation_status = "CORRECT" if is_correct else "INCORRECT"
        evaluation_confidence = None
        evaluation_reason = None
        needs_ai = False
    else:
        # mcq_single / true_false: EXACTLY the existing deterministic
        # check, completely unchanged — binary, full points or none.
        is_correct = _check_answer_correct(question, selected_option, selected_options)
        score = question.points if is_correct else 0
        evaluation_method = None
        evaluation_status = "CORRECT" if is_correct else "INCORRECT"
        evaluation_confidence = None
        evaluation_reason = None
        needs_ai = False

    response = StudentResponse(
        student_id=student.id,
        session_id=session.id,
        quiz_id=question.quiz_id,
        question_id=question.id,
        selected_option=stored_answer,
        is_correct=is_correct,
        score=score,
        response_time_ms=response_time_ms,
        submitted_at=submitted_at,
        evaluation_method=evaluation_method,
        evaluation_status=evaluation_status,
        evaluation_confidence=evaluation_confidence,
        evaluation_reason=evaluation_reason,
        # Historical snapshot (Issue 2) — see the Response model
        # docstring. Captured from the CURRENT Question row right now,
        # at the moment this student actually answered it — exactly
        # what they saw, immune to any later edit made to prepare a
        # future session.
        question_text_snapshot=question.question_text,
        question_type_snapshot=question.question_type,
        question_options_snapshot=question.options,
        correct_answer_snapshot=question.correct_answer,
        question_points_snapshot=question.points,
    )
    db.add(response)
    try:
        db.commit()
    except IntegrityError:
        # Two near-simultaneous requests for the same (student, session,
        # question) both passed the `existing` check above before either
        # had committed — a genuine race (duplicate submit click, a
        # retried request after a flaky connection), not hypothetical.
        # The UNIQUE constraint on (student_id, session_id, question_id)
        # is the real backstop; this converts that into the same clean
        # "already submitted" response the normal duplicate-detection
        # path returns, instead of surfacing a raw 500 mid-quiz.
        db.rollback()
        return {
            "success": True,
            "already_submitted": True,
            "message": "Answer submitted successfully.",
            "student_completed": _student_completed_quiz(db, student.id, question.quiz_id, session.id),
        }
    db.refresh(response)

    next_question_number = None
    if question.quiz.auto_advance:
        # This student's own personal pacing moves on immediately — no
        # waiting for the shared question timer, no waiting for any
        # other student, and the GLOBAL session.current_question_index
        # is never touched by this (see _student_effective_question_index
        # for how the teacher's existing manual controls still layer on
        # top of this without any changes to them).
        #
        # Only advances if the question just answered was genuinely this
        # student's OWN current question right now — guards against a
        # late/stale submission (e.g. a request that finally arrives
        # after this student's own timer already auto-submitted and
        # moved them on) advancing them a second time or out of order.
        quiz_questions = question.quiz.questions
        current_idx = student.current_question_index
        if 1 <= current_idx <= len(quiz_questions) and quiz_questions[current_idx - 1].id == question.id:
            student.current_question_index += 1
            student.question_started_at = datetime.now(timezone.utc)
            db.commit()
            if student.current_question_index <= len(quiz_questions):
                next_question_number = student.current_question_index

    if needs_ai:
        # Scheduled to run AFTER this HTTP response is sent — the student
        # is never kept waiting on an AI call. The Results page's
        # existing 2s auto-refresh will pick up the update once the
        # background task finishes (see _run_ai_grading_task).
        background_tasks.add_task(_run_ai_grading_task, response.id, question.id, stored_answer or "")

    # Completion is recalculated only AFTER this response is committed —
    # so it only ever becomes true once the final answer is actually saved.
    # A question still EVALUATING still counts as "answered" here (the
    # student submitted something; grading being pending doesn't block
    # their progression through the quiz).
    student_completed = _student_completed_quiz(db, student.id, question.quiz_id)

    return {
        "success": True,
        "already_submitted": False,
        "message": "Answer submitted successfully.",
        "student_completed": student_completed,
        # Only set when Auto Advance actually moved this student's own
        # pacing forward just now — lets the client jump straight to
        # the next question instead of waiting for the next poll cycle.
        # None (absent-equivalent in JS truthiness) in every existing
        # case, including every Auto-Advance-off submission.
        "next_question_number": next_question_number,
    }


@app.get("/student/session/{session_code}/waiting")
def session_waiting_next(
    session_code: str,
    request: Request,
    student_id: int | None = None,
    last_question: int = 0,
    db: Session = Depends(get_db),
):
    """Shown after a student submits an answer — holds here until the
    instructor's next action. Polls the same status endpoint as the
    waiting room and question player, comparing against `last_question`
    (the question this student just answered) so it only moves forward
    once the teacher actually advances past that question."""
    student = db.query(Student).filter(Student.id == student_id).first() if student_id else None
    if student and _student_completed_quiz(db, student.id, student.quiz_id):
        return RedirectResponse(url=f"/student/session/{session_code}/completed", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="session_waiting_next.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            "student": student,
            "session_code": session_code.upper(),
            "last_question": last_question,
        },
    )


@app.get("/student/session/{session_code}/completed")
def session_completed_page(
    session_code: str, request: Request, student_id: int | None = None, db: Session = Depends(get_db)
):
    """Reached once the teacher clicks "Finish Quiz" and every question
    is done — distinct from Session Ended, which covers an early/manual
    stop via "End Session" before all questions were used."""
    session = (
        db.query(LiveSession)
        .filter(LiveSession.session_code == session_code.upper())
        .first()
    )
    student = db.query(Student).filter(Student.id == student_id).first() if student_id else None
    return templates.TemplateResponse(
        request=request,
        name="session_completed.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            "quiz": session.quiz if session else None,
            "results_url": (
                f"/student/results/{student.id}?token={student.result_token}"
                if student and student.result_token else None
            ),
        },
    )


@app.get("/student/results/{student_id}")
def student_results_page(
    student_id: int, request: Request, token: str = "", db: Session = Depends(get_db)
):
    """A student's own results — nothing else. This is the server-side
    enforcement of "students must only see their own results": the
    student_id in the URL is NOT sufficient on its own (it's a
    sequential integer, trivially guessable/incrementable), so this
    additionally requires the matching result_token — a random,
    unguessable value generated at join time (see /join) that isn't
    derivable from the student_id.

    Every failure path (no such student, no token configured, wrong
    token) returns the SAME 404, with the SAME message, deliberately —
    distinguishing "wrong token" from "no such student" would let
    someone probe for valid student_ids by watching which error comes
    back, even without ever guessing a correct token.

    hmac.compare_digest is used instead of `==` so that even the TIME
    the comparison takes can't leak how many leading characters of a
    guessed token happened to match — an ordinary string `==` compare
    exits early on the first mismatched character, which is a real
    timing side-channel for security tokens.
    """
    student = db.query(Student).filter(Student.id == student_id).first()
    token_ok = bool(
        student and student.result_token and token
        and hmac.compare_digest(token, student.result_token)
    )
    if not token_ok:
        raise HTTPException(status_code=404, detail="Results not found.")

    data = _student_own_results(db, student)
    return templates.TemplateResponse(
        request=request,
        name="student_results.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            **data,
        },
    )


@app.get("/student/session/{session_code}/ended")
def session_ended_page(session_code: str, request: Request, db: Session = Depends(get_db)):
    session = (
        db.query(LiveSession)
        .filter(LiveSession.session_code == session_code.upper())
        .first()
    )
    return templates.TemplateResponse(
        request=request,
        name="session_ended.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            "quiz": session.quiz if session else None,
        },
    )


# ---------------------------------------------------------------- dashboard --

@app.get("/admin")
def admin_page(request: Request, success: str | None = None, db: Session = Depends(get_db)):
    """Teacher dashboard. Quiz stats, question counts, student counts, and
    the Recent Quizzes table all come from SQLite now."""
    quizzes = db.query(Quiz).order_by(Quiz.created_at.desc()).all()
    # "Completed" is no longer a quiz-level status a quiz can get stuck
    # in (a quiz reverts to Published once its session ends, so it can
    # be reused — see finish_quiz/end_session). This stat now reflects
    # a historical fact instead: how many distinct quizzes have EVER had
    # a session actually run to completion. That's the more accurate
    # meaning of "completed quizzes" anyway, and — unlike the old
    # quiz.status check — it stays correct even after a quiz is reused
    # for a second session, rather than dropping back to uncounted.
    completed_count = (
        db.query(Quiz.id)
        .join(LiveSession, LiveSession.quiz_id == Quiz.id)
        .filter(LiveSession.status == "Completed")
        .distinct()
        .count()
    )
    active_quiz_count = sum(1 for q in quizzes if q.status == "Live")
    connected_students = db.query(Student).filter(Student.status != "Disconnected").count()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "dashboard",
            "quizzes": quizzes,
            "total_quizzes": len(quizzes),
            "completed_quizzes": completed_count,
            "active_quiz_count": active_quiz_count,
            "connected_students": connected_students,
            "status_badge_class": STATUS_BADGE_CLASS,
            "success_message": SUCCESS_MESSAGES.get(success),
        },
    )


# ----------------------------------------------------------------- settings --

@app.get("/admin/settings")
def settings_page(request: Request, success: str | None = None, error: str | None = None):
    """Authenticated-only (enforced by the /admin auth middleware, same
    as every other teacher route). Currently just the password-change
    form; AppSetting is deliberately generic (see models.py) so future
    classroom settings can be added here without another migration."""
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "settings",
            "success_message": "Password updated successfully." if success == "password_updated" else None,
            "error_message": error,
        },
    )


@app.post("/admin/settings/password")
def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    stored_hash = auth.get_password_hash(db)
    if not stored_hash or not auth.verify_password(current_password, stored_hash):
        return RedirectResponse(
            url="/admin/settings?error=Current+password+is+incorrect.", status_code=303
        )
    if len(new_password) < 8:
        return RedirectResponse(
            url="/admin/settings?error=New+password+must+be+at+least+8+characters.", status_code=303
        )
    if new_password != confirm_password:
        return RedirectResponse(
            url="/admin/settings?error=New+password+and+confirmation+do+not+match.", status_code=303
        )
    auth.set_password_hash(db, auth.hash_password(new_password))
    return RedirectResponse(url="/admin/settings?success=password_updated", status_code=303)


# --------------------------------------------------------------- quiz: create --

@app.get("/admin/quizzes/new")
def create_quiz_page(request: Request):
    """Blank Create Quiz form."""
    return templates.TemplateResponse(
        request=request,
        name="quiz_form.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "quizzes",
            "mode": "create",
            "quiz": None,
            "form_action": "/admin/quizzes/new",
        },
    )


@app.post("/admin/quizzes/new")
def create_quiz_submit(
    title: str = Form(...),
    description: str = Form(""),
    duration_minutes: int = Form(10),
    passing_percentage: int = Form(60),
    auto_advance: bool = Form(False),
    exam_mode: bool = Form(False),
    db: Session = Depends(get_db),
):
    quiz = Quiz(
        title=title.strip() or "Untitled Quiz",
        description=description.strip(),
        duration_minutes=duration_minutes,
        passing_percentage=passing_percentage,
        auto_advance=auto_advance,
        exam_mode=exam_mode,
        status="Draft",
        created_at=datetime.now(timezone.utc),
    )
    db.add(quiz)
    db.commit()
    return RedirectResponse(url="/admin?success=created", status_code=303)


# ------------------------------------------------------- quiz: question builder --

@app.get("/admin/quizzes/{quiz_id}")
def quiz_builder_page(
    quiz_id: int, request: Request, success: str | None = None, db: Session = Depends(get_db)
):
    """The Question Builder: quiz summary + Preview/Publish/Back actions +
    the list of questions belonging to this quiz, plus this quiz's
    session history — a quiz can now be reused for many independent
    live sessions over time (see finish_quiz/end_session), so every
    past session's own results need a discoverable link, not just a
    URL a teacher would have to already know."""
    quiz = _get_quiz_or_404(db, quiz_id)
    past_sessions = (
        db.query(LiveSession)
        .filter(LiveSession.quiz_id == quiz_id)
        .order_by(LiveSession.started_at.desc())
        .all()
    )
    session_student_counts = {
        row[0]: row[1]
        for row in (
            db.query(Student.session_id, func.count(Student.id))
            .filter(Student.session_id.in_([s.id for s in past_sessions]))
            .group_by(Student.session_id)
            .all()
        )
    } if past_sessions else {}
    return templates.TemplateResponse(
        request=request,
        name="quiz_builder.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "quizzes",
            "quiz": quiz,
            "questions": quiz.questions,
            "question_type_labels": QUESTION_TYPES,
            "status_badge_class": STATUS_BADGE_CLASS,
            "success_message": SUCCESS_MESSAGES.get(success),
            "past_sessions": past_sessions,
            "session_student_counts": session_student_counts,
        },
    )


@app.post("/admin/quizzes/{quiz_id}/publish")
def publish_quiz(quiz_id: int, db: Session = Depends(get_db)):
    """Toggle a quiz between Draft and Published. Only meaningful before a
    session has ever gone Live — once a quiz is Live/Completed/Archived,
    its lifecycle is driven from the Live Sessions page instead."""
    quiz = _get_quiz_or_404(db, quiz_id)
    if quiz.status == "Draft":
        quiz.status = "Published"
        success = "published"
    elif quiz.status == "Published":
        quiz.status = "Draft"
        success = "unpublished"
    else:
        # Live / Completed / Archived — publish/unpublish no longer applies
        return RedirectResponse(url=f"/admin/quizzes/{quiz_id}", status_code=303)
    db.commit()
    return RedirectResponse(url=f"/admin/quizzes/{quiz_id}?success={success}", status_code=303)


# ----------------------------------------------------------------- quiz: edit --

@app.get("/admin/quizzes/{quiz_id}/edit")
def edit_quiz_page(quiz_id: int, request: Request, db: Session = Depends(get_db)):
    quiz = _get_quiz_or_404(db, quiz_id)
    return templates.TemplateResponse(
        request=request,
        name="quiz_form.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "quizzes",
            "mode": "edit",
            "quiz": quiz,
            "form_action": f"/admin/quizzes/{quiz_id}/edit",
        },
    )


@app.post("/admin/quizzes/{quiz_id}/edit")
def edit_quiz_submit(
    quiz_id: int,
    title: str = Form(...),
    description: str = Form(""),
    duration_minutes: int = Form(10),
    passing_percentage: int = Form(60),
    auto_advance: bool = Form(False),
    exam_mode: bool = Form(False),
    db: Session = Depends(get_db),
):
    quiz = _get_quiz_or_404(db, quiz_id)
    active_exam_session = _has_active_exam_session(db, quiz.id)
    quiz.title = title.strip() or "Untitled Quiz"
    quiz.description = description.strip()
    quiz.duration_minutes = duration_minutes
    quiz.passing_percentage = passing_percentage
    if not active_exam_session:
        quiz.auto_advance = auto_advance
    if not _get_current_session(db, quiz.id):
        quiz.exam_mode = exam_mode
    db.commit()
    return RedirectResponse(url="/admin?success=updated", status_code=303)


# --------------------------------------------------------------- quiz: delete --

@app.post("/admin/quizzes/{quiz_id}/delete")
def delete_quiz(quiz_id: int, db: Session = Depends(get_db)):
    quiz = _get_quiz_or_404(db, quiz_id)
    _ensure_exam_questions_mutable(db, quiz.id)
    db.delete(quiz)  # cascades to its questions via the ORM relationship
    db.commit()
    return RedirectResponse(url="/admin?success=deleted", status_code=303)


# ----------------------------------------------------------- question: create --

@app.get("/admin/quizzes/{quiz_id}/questions/new")
def create_question_page(quiz_id: int, request: Request, error: str | None = None, db: Session = Depends(get_db)):
    quiz = _get_quiz_or_404(db, quiz_id)
    return templates.TemplateResponse(
        request=request,
        name="question_form.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "quizzes",
            "mode": "create",
            "quiz": quiz,
            "question": None,
            "question_types": QUESTION_TYPES,
            "form_action": f"/admin/quizzes/{quiz_id}/questions/new",
            "error_message": error,
            "duration_min": QUESTION_DURATION_MIN_SECONDS,
            "duration_max": QUESTION_DURATION_MAX_SECONDS,
            "prefill": {
                "option_a": "", "option_b": "", "option_c": "", "option_d": "",
                "correct_single": "", "correct_flags": {"A": False, "B": False, "C": False, "D": False},
                "correct_true_false": "True", "accepted_answers": "", "grading_notes": "",
                "duration_seconds": 600,
            },
        },
    )


@app.post("/admin/quizzes/{quiz_id}/questions/new")
def create_question_submit(
    quiz_id: int,
    question_text: str = Form(...),
    question_type: str = Form(...),
    points: int = Form(1),
    duration_seconds: str = Form("600"),
    option_a: str = Form(""), option_b: str = Form(""),
    option_c: str = Form(""), option_d: str = Form(""),
    correct_single: str = Form(""),
    option_a_correct: bool = Form(False), option_b_correct: bool = Form(False),
    option_c_correct: bool = Form(False), option_d_correct: bool = Form(False),
    correct_true_false: str = Form("True"),
    accepted_answers: str = Form(""),
    grading_notes: str = Form(""),
    image: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    _ensure_exam_questions_mutable(db, quiz_id)
    quiz = _get_quiz_or_404(db, quiz_id)

    validated_duration, duration_error = _validate_question_duration(duration_seconds)
    if duration_error:
        return RedirectResponse(
            url=f"/admin/quizzes/{quiz_id}/questions/new?error={duration_error}", status_code=303
        )

    options_list, correct_value = _build_question_payload(
        question_type, option_a, option_b, option_c, option_d, correct_single,
        option_a_correct, option_b_correct, option_c_correct, option_d_correct,
        correct_true_false, accepted_answers,
    )
    question = Question(
        quiz_id=quiz.id,
        question_text=question_text.strip(),
        question_type=question_type,
        points=max(points, 1),
        duration_seconds=validated_duration,
        options=json.dumps(options_list),
        correct_answer=json.dumps(correct_value),
        image_path=_save_uploaded_image(image),
        created_at=datetime.now(timezone.utc),
        grading_notes=(grading_notes.strip() or None) if question_type == "short_answer" else None,
    )
    db.add(question)
    db.commit()
    return RedirectResponse(url=f"/admin/quizzes/{quiz_id}?success=question_created", status_code=303)


# ------------------------------------------------------------- question: edit --

@app.get("/admin/quizzes/{quiz_id}/questions/{question_id}/edit")
def edit_question_page(
    quiz_id: int, question_id: int, request: Request, error: str | None = None, db: Session = Depends(get_db)
):
    quiz = _get_quiz_or_404(db, quiz_id)
    question = _get_question_or_404(db, quiz_id, question_id)
    return templates.TemplateResponse(
        request=request,
        name="question_form.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "quizzes",
            "mode": "edit",
            "quiz": quiz,
            "question": question,
            "question_types": QUESTION_TYPES,
            "form_action": f"/admin/quizzes/{quiz_id}/questions/{question_id}/edit",
            "error_message": error,
            "duration_min": QUESTION_DURATION_MIN_SECONDS,
            "duration_max": QUESTION_DURATION_MAX_SECONDS,
            "prefill": _question_prefill(question),
        },
    )


@app.post("/admin/quizzes/{quiz_id}/questions/{question_id}/edit")
def edit_question_submit(
    quiz_id: int,
    question_id: int,
    question_text: str = Form(...),
    question_type: str = Form(...),
    points: int = Form(1),
    duration_seconds: str = Form("600"),
    option_a: str = Form(""), option_b: str = Form(""),
    option_c: str = Form(""), option_d: str = Form(""),
    correct_single: str = Form(""),
    option_a_correct: bool = Form(False), option_b_correct: bool = Form(False),
    option_c_correct: bool = Form(False), option_d_correct: bool = Form(False),
    correct_true_false: str = Form("True"),
    accepted_answers: str = Form(""),
    grading_notes: str = Form(""),
    image: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    _ensure_exam_questions_mutable(db, quiz_id)
    question = _get_question_or_404(db, quiz_id, question_id)

    validated_duration, duration_error = _validate_question_duration(duration_seconds)
    if duration_error:
        return RedirectResponse(
            url=f"/admin/quizzes/{quiz_id}/questions/{question_id}/edit?error={duration_error}",
            status_code=303,
        )

    options_list, correct_value = _build_question_payload(
        question_type, option_a, option_b, option_c, option_d, correct_single,
        option_a_correct, option_b_correct, option_c_correct, option_d_correct,
        correct_true_false, accepted_answers,
    )
    question.question_text = question_text.strip()
    question.question_type = question_type
    question.points = max(points, 1)
    question.duration_seconds = validated_duration
    question.options = json.dumps(options_list)
    question.correct_answer = json.dumps(correct_value)
    question.grading_notes = (grading_notes.strip() or None) if question_type == "short_answer" else None

    new_image_path = _save_uploaded_image(image)
    if new_image_path:
        question.image_path = new_image_path

    db.commit()
    return RedirectResponse(url=f"/admin/quizzes/{quiz_id}?success=question_updated", status_code=303)


# ---------------------------------------------------------- question: duplicate --

@app.post("/admin/quizzes/{quiz_id}/questions/{question_id}/duplicate")
def duplicate_question(quiz_id: int, question_id: int, db: Session = Depends(get_db)):
    _ensure_exam_questions_mutable(db, quiz_id)
    original = _get_question_or_404(db, quiz_id, question_id)
    copy = Question(
        quiz_id=quiz_id,
        question_text=original.question_text,
        question_type=original.question_type,
        points=original.points,
        options=original.options,
        correct_answer=original.correct_answer,
        image_path=original.image_path,
        created_at=datetime.now(timezone.utc),
    )
    db.add(copy)
    db.commit()
    return RedirectResponse(url=f"/admin/quizzes/{quiz_id}?success=question_duplicated", status_code=303)


# ----------------------------------------------------------- question: delete --

@app.post("/admin/quizzes/{quiz_id}/questions/{question_id}/delete")
def delete_question(quiz_id: int, question_id: int, db: Session = Depends(get_db)):
    _ensure_exam_questions_mutable(db, quiz_id)
    question = _get_question_or_404(db, quiz_id, question_id)
    db.delete(question)
    db.commit()
    return RedirectResponse(url=f"/admin/quizzes/{quiz_id}?success=question_deleted", status_code=303)


# ------------------------------------------------------------------ students --

@app.get("/admin/students")
def students_page(request: Request, success: str | None = None, db: Session = Depends(get_db)):
    students = db.query(Student).order_by(Student.connected_at.desc()).all()
    connected_count = sum(1 for s in students if s.status != "Disconnected")
    return templates.TemplateResponse(
        request=request,
        name="students.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "students",
            "students": students,
            "connected_count": connected_count,
            "success_message": SUCCESS_MESSAGES.get(success),
        },
    )


@app.post("/admin/students/{student_id}/remove")
def remove_student(student_id: int, db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    db.delete(student)
    db.commit()
    return RedirectResponse(url="/admin/students?success=removed", status_code=303)


# ------------------------------------------------------- classroom radio: library --
# Teacher-only (enforced by the existing /admin auth middleware — no
# separate check needed here). Manages the reusable music library,
# independent of any single quiz or live session; a Session merely
# SELECTS one of these tracks (see the radio-select route further down).

@app.get("/admin/music")
def music_library_page(request: Request, success: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    tracks = db.query(MusicTrack).order_by(MusicTrack.uploaded_at.desc()).all()
    # A track currently selected by any LIVE session can't be deleted
    # (see the delete route) — surfaced here so the UI can grey out that
    # action instead of letting the teacher hit a confusing error.
    in_use_track_ids = {
        s.radio_track_id
        for s in db.query(LiveSession).filter(LiveSession.status == "Live", LiveSession.radio_track_id.isnot(None))
    }
    return templates.TemplateResponse(
        request=request,
        name="music_library.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "music",
            "tracks": tracks,
            "in_use_track_ids": in_use_track_ids,
            "max_size_mb": MAX_AUDIO_FILE_SIZE_BYTES // (1024 * 1024),
            "allowed_extensions": ", ".join(sorted(ALLOWED_AUDIO_EXTENSIONS)),
            "success_message": "Track uploaded successfully." if success == "uploaded"
                else "Track deleted successfully." if success == "deleted" else None,
            "error_message": error,
        },
    )


@app.post("/admin/music/upload")
def music_upload_submit(
    display_name: str = Form(""),
    audio_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    storage_filename, duration, size, error = _validate_and_save_audio_upload(audio_file)
    if error:
        return RedirectResponse(url=f"/admin/music?error={error}", status_code=303)

    name = display_name.strip() or Path(audio_file.filename).stem
    track = MusicTrack(
        display_name=name,
        storage_filename=storage_filename,
        duration_seconds=duration,
        file_size_bytes=size,
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(track)
    db.commit()
    return RedirectResponse(url="/admin/music?success=uploaded", status_code=303)


@app.post("/admin/music/{track_id}/delete")
def music_delete_submit(track_id: int, db: Session = Depends(get_db)):
    track = db.query(MusicTrack).filter(MusicTrack.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    # Refuse to delete a track a LIVE session currently has selected —
    # the audio file disappearing mid-class would break playback for
    # every connected student with no way to recover except picking a
    # different track. Deletion is fine once no live session references
    # it (a Completed session's historical radio_track_id can dangle
    # harmlessly — nothing reads it for a finished session).
    in_use = (
        db.query(LiveSession)
        .filter(LiveSession.radio_track_id == track.id, LiveSession.status == "Live")
        .first()
    )
    if in_use:
        return RedirectResponse(
            url="/admin/music?error=This+track+is+selected+for+an+active+live+session+and+can%27t+be+deleted+right+now.",
            status_code=303,
        )

    filepath = AUDIO_UPLOADS_DIR / track.storage_filename
    filepath.unlink(missing_ok=True)
    db.delete(track)
    db.commit()
    return RedirectResponse(url="/admin/music?success=deleted", status_code=303)


# -------------------------------------------------------------------- results --

def _student_own_results(db: Session, student: Student) -> dict:
    """Per-student result data — ONLY this one student's own responses,
    never another student's, never the class leaderboard, never teacher
    analytics. This is the sole data source for the student results
    page; the route calling this additionally verifies the student's
    unguessable result_token before ever reaching here (see
    student_results_page) — the privacy boundary is enforced in the
    route, this function just doesn't have access to anyone else's data
    in the first place, by construction (a single `student` is passed
    in, not a session_id or quiz_id that could be widened later)."""
    responses = (
        db.query(StudentResponse)
        .filter(StudentResponse.student_id == student.id)
        .all()
    )
    by_question_id = {r.question_id: r for r in responses}
    quiz = student.quiz
    quiz_questions = quiz.questions if quiz else []

    pending_statuses = ("NEEDS_REVIEW", "EVALUATING")
    breakdown = []
    for q in quiz_questions:
        r = by_question_id.get(q.id)
        # Prefer the response's own historical snapshot — exactly what
        # this student was actually shown and what counted as correct
        # at the time — over the live Question row, which may since
        # have been edited to prepare a different session. See the
        # Response model docstring. Only reachable for an ANSWERED
        # question; an unanswered one has no snapshot to fall back to
        # (nothing was ever captured for it) and still reflects the
        # live question — a narrow, documented limitation, not a
        # silent gap: there is no historical data to show for a
        # question this student never actually saw graded.
        display_text = (r.question_text_snapshot if r and r.question_text_snapshot else q.question_text)
        display_points = (r.question_points_snapshot if r and r.question_points_snapshot is not None else q.points)
        breakdown.append({
            "question_text": display_text,
            "question_type": QUESTION_TYPES.get(
                (r.question_type_snapshot if r and r.question_type_snapshot else q.question_type),
                q.question_type,
            ),
            "points": display_points,
            "answered": r is not None,
            "your_answer": r.selected_option if r else None,
            "awarded": r.score if r else 0,
            "is_correct": r.is_correct if r else False,
            "evaluation_status": r.evaluation_status if r else ("UNANSWERED" if not r else None),
            "response_time_seconds": (
                round(r.response_time_ms / 1000, 2) if r and r.response_time_ms is not None else None
            ),
        })

    total_score = sum(r.score for r in responses)
    correct_count = sum(1 for r in responses if r.is_correct)
    needs_review_count = sum(1 for r in responses if r.evaluation_status in pending_statuses)
    incorrect_count = len(responses) - correct_count - needs_review_count
    # Computed from what's actually displayed above (each row's own
    # resolved points, snapshot-preferred) rather than a separate live
    # sum — keeps the percentage internally consistent with the
    # breakdown a student is looking at, even for an edited quiz.
    max_possible = sum(row["points"] for row in breakdown)
    percentage = round((total_score / max_possible) * 100, 2) if max_possible else 0

    return {
        "student": student,
        "quiz": quiz,
        "total_score": total_score,
        "max_possible": max_possible,
        "percentage": percentage,
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "needs_review_count": needs_review_count,
        "total_questions": len(quiz_questions),
        "answered_count": len(responses),
        "breakdown": breakdown,
    }


def _compute_results(db: Session, session_id: int | None = None) -> dict:
    """Aggregate stats + leaderboard for ONE specific competition attempt:
    Quiz -> Live Session -> Student Attempt -> Responses. Never mixes
    different sessions/competitions together — every student and every
    response counted here belongs to exactly the target session.

    If session_id isn't given, defaults to the most recently STARTED
    session across the whole app, so the existing Results page keeps
    showing "the current competition" with no picker UI yet (that's a
    future step) while the underlying computation is fully session-scoped.

    Competition score, per student attempt:
      - score (earned points)   = sum of that student's Response.score
                                   (each response scores the question's
                                   own `points` if correct, else 0 for
                                   binary types — mcq_multiple can score
                                   a proportional amount between 0 and
                                   points, see _score_mcq_multiple. A
                                   NEEDS_REVIEW/EVALUATING response
                                   scores 0 provisionally, per Part 2 —
                                   no partial credit at this stage.)
      - max_possible             = sum of every question's `points` in
                                   this quiz (what a perfect attempt earns)
      - percentage                = score / max_possible * 100
      - correct / incorrect / needs_review / unanswered counts — a
        response still pending AI judgment is NOT the same thing as a
        confidently wrong one, so it's tracked separately rather than
        silently folded into "incorrect"
      - avg / total response time (seconds, only over questions actually
        answered — an unanswered question contributes no response time)

    Ranking (leaderboard order): higher score first; ties broken by more
    correct answers; remaining ties broken by lower average response
    time (faster only matters once score and correctness are equal — a
    faster-but-lower-scoring student can never outrank a higher scorer).
    A student with no timed responses at all sorts last among ties on
    that criterion, never ahead of someone with a real time. A complete
    tie on all three falls back to student_id as a final, stable,
    deterministic tie-break rather than arbitrary/incidental ordering.
    """
    if session_id is not None:
        target_session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    else:
        target_session = db.query(LiveSession).order_by(LiveSession.started_at.desc()).first()

    if not target_session:
        return {
            "session_id": None, "quiz_id": None, "quiz_title": None,
            "total_students": 0, "submitted_answers": 0, "correct_answers": 0,
            "incorrect_answers": 0, "needs_review_answers": 0, "needs_review_items": [],
            "submission_event_count": 0, "submission_events": [],
            "max_possible_per_student": 0,
            "average_score": 0, "average_response_time_seconds": 0,
            "leaderboard": [],
        }

    quiz = target_session.quiz
    total_questions = len(quiz.questions)
    max_possible_per_student = sum(q.points for q in quiz.questions)

    students = (
        db.query(Student)
        .filter(Student.session_id == target_session.id)
        .order_by(Student.connected_at)
        .all()
    )
    responses = (
        db.query(StudentResponse)
        .filter(StudentResponse.session_id == target_session.id)
        .all()
    )
    submission_event_count = (
        db.query(SubmissionEvent)
        .filter(SubmissionEvent.session_id == target_session.id)
        .count()
    )
    submission_events = (
        db.query(SubmissionEvent)
        .filter(SubmissionEvent.session_id == target_session.id)
        .order_by(SubmissionEvent.server_received_at.desc(), SubmissionEvent.id.desc())
        .limit(200)
        .all()
    )

    submitted_answers = len(responses)
    correct_answers = sum(1 for r in responses if r.is_correct)
    # NEEDS_REVIEW / EVALUATING responses have is_correct=False (no points
    # awarded yet) but are NOT the same thing as a confidently wrong
    # answer — Part 5 asks these to be distinguishable, so they're pulled
    # out of "incorrect" here rather than silently lumped in with it.
    pending_statuses = ("NEEDS_REVIEW", "EVALUATING")
    needs_review_answers = sum(1 for r in responses if r.evaluation_status in pending_statuses)
    incorrect_answers = submitted_answers - correct_answers - needs_review_answers
    average_score = round(sum(r.score for r in responses) / submitted_answers, 1) if submitted_answers else 0

    timed = [r.response_time_ms for r in responses if r.response_time_ms is not None]
    average_response_time_seconds = round((sum(timed) / len(timed)) / 1000, 2) if timed else 0

    # Flat list for the minimal teacher review capability (Part 7): every
    # response still sitting at NEEDS_REVIEW or EVALUATING, with enough
    # context to judge it without a separate query per row.
    needs_review_items = []
    if needs_review_answers:
        student_by_id = {s.id: s for s in students}
        question_by_id = {q.id: q for q in quiz.questions}
        for r in responses:
            if r.evaluation_status not in pending_statuses:
                continue
            q = question_by_id.get(r.question_id)
            s = student_by_id.get(r.student_id)
            # Prefer the historical snapshot (exactly what this student
            # saw when they answered) over the live Question row, which
            # may since have been edited to prepare a different session
            # — see the Response model docstring for why this matters.
            # Only pre-existing responses (created before this snapshot
            # field existed) fall back to the live object.
            question_text = r.question_text_snapshot or (q.question_text if q else "(question removed)")
            correct_answer_raw = r.correct_answer_snapshot if r.correct_answer_snapshot is not None else (q.correct_answer if q else None)
            accepted_raw = json.loads(correct_answer_raw or "null")
            expected = accepted_raw[0] if isinstance(accepted_raw, list) and accepted_raw else None
            needs_review_items.append({
                "response_id": r.id,
                "question_text": question_text,
                "student_name": s.name if s else "(student removed)",
                "student_answer": r.selected_option,
                "expected_answer": expected,
                "evaluation_status": r.evaluation_status,
                "evaluation_method": r.evaluation_method,
                "evaluation_confidence": r.evaluation_confidence,
                "evaluation_reason": r.evaluation_reason,
            })

    student_by_id = {student.id: student for student in students}
    question_by_id = {question.id: question for question in quiz.questions}
    submission_event_items = []
    for event in submission_events:
        student = student_by_id.get(event.student_id)
        submitted_question = question_by_id.get(event.submitted_question_id)
        authoritative_question = question_by_id.get(event.authoritative_question_id)
        submission_event_items.append({
            "event_id": event.id,
            "event_type": event.event_type,
            "student_name": student.name if student else f"Student #{event.student_id}",
            "roll_number": student.roll_number if student else "—",
            "submitted_question_number": event.submitted_question_number,
            "submitted_question": (
                submitted_question.question_text if submitted_question
                else f"Question #{event.submitted_question_id}"
            ),
            "authoritative_question": (
                authoritative_question.question_text if authoritative_question else "—"
            ),
            "server_received_at": _to_utc_iso_z(event.server_received_at),
            "deadline": _to_utc_iso_z(event.deadline) if event.deadline else "—",
            "response_id": event.response_id,
        })

    by_student: dict[int, list] = {}
    for r in responses:
        by_student.setdefault(r.student_id, []).append(r)

    leaderboard = []
    for student in students:
        student_responses = by_student.get(student.id, [])
        score = sum(r.score for r in student_responses)
        correct = sum(1 for r in student_responses if r.is_correct)
        pending = sum(1 for r in student_responses if r.evaluation_status in pending_statuses)
        # A response pending AI review isn't confidently wrong — it just
        # hasn't been decided yet — so it must not be counted as
        # "incorrect" here, same distinction the global stat above makes.
        incorrect = sum(
            1 for r in student_responses
            if not r.is_correct and r.evaluation_status not in pending_statuses
        )
        answered = len(student_responses)
        unanswered = max(total_questions - answered, 0)
        percentage = (
            round((score / max_possible_per_student) * 100, 2) if max_possible_per_student else 0
        )
        s_timed = [r.response_time_ms for r in student_responses if r.response_time_ms is not None]
        avg_rt = round((sum(s_timed) / len(s_timed)) / 1000, 2) if s_timed else None
        total_rt = round(sum(s_timed) / 1000, 2) if s_timed else 0
        leaderboard.append({
            "student_id": student.id,
            "name": student.name,
            "roll_number": student.roll_number,
            "course": student.course,
            "score": score,
            "max_possible": max_possible_per_student,
            "percentage": percentage,
            "correct": correct,
            "incorrect": incorrect,
            "needs_review": pending,
            "unanswered": unanswered,
            "avg_response_time_seconds": avg_rt,
            "total_response_time_seconds": total_rt,
        })

    # Priority: (1) higher score, (2) more correct, (3) lower avg response
    # time, (4) student_id as a final deterministic tie-break so a
    # complete tie never falls back to incidental/arbitrary ordering.
    # Python's sort is stable and tuples compare element-by-element, so
    # negating the "higher is better" fields lets one ascending sort
    # express the whole priority order in one pass. A None avg time (never
    # answered anything timed) is treated as +inf so it never beats a real
    # time on the speed tie-break.
    leaderboard.sort(key=lambda row: (
        -row["score"],
        -row["correct"],
        row["avg_response_time_seconds"] if row["avg_response_time_seconds"] is not None else float("inf"),
        row["student_id"],
    ))
    for i, row in enumerate(leaderboard, start=1):
        row["rank"] = i

    return {
        "session_id": target_session.id,
        "quiz_id": quiz.id,
        "quiz_title": quiz.title,
        "total_students": len(students),
        "submitted_answers": submitted_answers,
        "correct_answers": correct_answers,
        "incorrect_answers": incorrect_answers,
        "needs_review_answers": needs_review_answers,
        "needs_review_items": needs_review_items,
        "exam_mode": quiz.exam_mode,
        "submission_event_count": submission_event_count,
        "submission_events": submission_event_items,
        "max_possible_per_student": max_possible_per_student,
        "average_score": average_score,
        "average_response_time_seconds": average_response_time_seconds,
        "leaderboard": leaderboard,
    }


@app.get("/admin/results")
def results_page(request: Request, session_id: int | None = None, db: Session = Depends(get_db)):
    data = _compute_results(db, session_id=session_id)
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "results",
            **data,
        },
    )


@app.get("/admin/results/data")
def results_data(session_id: int | None = None, db: Session = Depends(get_db)):
    """Polled every 2s by the Results page — plain JSON, same shape used
    to render the page server-side, so the frontend can rebuild the stat
    cards and leaderboard table from it without a full page reload."""
    return _compute_results(db, session_id=session_id)


@app.get("/student/results/{student_id}")
def student_results_page(
    student_id: int, request: Request, token: str = "", db: Session = Depends(get_db)
):
    """A student's own results — ONLY their own, enforced server-side
    (Part 5's explicit requirement — this must NOT be achievable by
    hiding a link with CSS/JS). Deliberately public (not under /admin,
    so the teacher auth middleware doesn't apply here — students have no
    login system), but gated by result_token: a random, unguessable
    value generated at join time (see /join), required alongside the
    student_id in the URL. Knowing/incrementing a sequential student_id
    alone is not enough to view someone else's results.

    The 404 is identical whether the student doesn't exist, has no
    token yet, or the token is simply wrong — never revealing which
    case it was, so this can't be used to probe for valid student IDs.
    Token comparison is constant-time (hmac.compare_digest) so response
    timing can't leak how much of the token matched."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student or not student.result_token or not token:
        raise HTTPException(status_code=404, detail="Results not found")
    if not hmac.compare_digest(token, student.result_token):
        raise HTTPException(status_code=404, detail="Results not found")

    data = _student_own_results(db, student)
    return templates.TemplateResponse(
        request=request,
        name="student_results.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "student",
            **data,
        },
    )


@app.post("/admin/results/review/{response_id}")
def review_response(
    response_id: int,
    decision: str = Form(...),
    session_id: int | None = None,
    db: Session = Depends(get_db),
):
    """Minimal teacher review action for a NEEDS_REVIEW / EVALUATING
    response (Part 7) — the teacher's decision is final and overwrites
    whatever the AI (or the pending placeholder) had. Deliberately just
    two buttons (Correct / Incorrect) rather than a full review UI,
    since the data needed for a richer one is already exposed via
    needs_review_items and can be built later without further backend
    changes."""
    response = db.query(StudentResponse).filter(StudentResponse.id == response_id).first()
    if not response:
        raise HTTPException(status_code=404, detail="Response not found")
    question = db.query(Question).filter(Question.id == response.question_id).first()
    if decision not in ("CORRECT", "INCORRECT"):
        raise HTTPException(status_code=422, detail="decision must be CORRECT or INCORRECT")

    response.is_correct = decision == "CORRECT"
    response.score = (question.points if question else 0) if response.is_correct else 0
    response.evaluation_status = decision
    response.evaluation_method = "teacher_review"
    db.commit()

    redirect_url = "/admin/results" + (f"?session_id={session_id}" if session_id else "")
    return RedirectResponse(url=redirect_url, status_code=303)


# -------------------------------------------------------------- live sessions --

@app.get("/admin/sessions")
def live_sessions_page(request: Request, success: str | None = None, db: Session = Depends(get_db)):
    """Every Published or Live quiz, each paired with its current session
    (if it has one) so the page can show Start/End Session, the QR code,
    the join link, and a live connected-student count."""
    quizzes = (
        db.query(Quiz)
        .filter(Quiz.status.in_(["Published", "Live"]))
        .order_by(Quiz.created_at.desc())
        .all()
    )
    # Classroom Radio's music library — the same list is offered on every
    # row's track selector, independent of quiz phase (a session's radio
    # is a separate live system from question progression, see
    # PHASE_2_CLASSROOM_RADIO.md).
    music_tracks = db.query(MusicTrack).order_by(MusicTrack.display_name).all()

    rows = []
    for quiz in quizzes:
        session = _get_current_session(db, quiz.id) if quiz.status == "Live" else None
        phase = _session_phase(quiz, session)
        connected_count = 0
        answered_count = 0
        join_link = None
        question_ends_at = None
        next_action_url = None
        radio_state = None
        if session:
            connected_count = (
                db.query(Student)
                .filter(Student.session_id == session.id, Student.status != "Disconnected")
                .count()
            )
            host = request.url.hostname or "127.0.0.1"
            port = f":{request.url.port}" if request.url.port else ""
            join_link = f"{request.url.scheme}://{host}{port}/join/{session.session_code}"
            # Available on every phase — the radio is independent of
            # question progression, so it's shown regardless of whether
            # the session is WAITING, on a question, or QUIZ_COMPLETED.
            radio_state = _radio_effective_state(session)

            if phase.startswith("QUESTION_"):
                question_ends_at = _question_ends_at(session)
                next_action_url = f"/admin/quizzes/{quiz.id}/sessions/next-question"
            elif phase == "QUIZ_COMPLETED":
                answered_count = (
                    db.query(StudentResponse.student_id)
                    .filter(StudentResponse.session_id == session.id)
                    .distinct()
                    .count()
                )
        rows.append({
            "quiz": quiz,
            "session": session,
            "phase": phase,
            "connected_count": connected_count,
            "answered_count": answered_count,
            "join_link": join_link,
            "question_ends_at": question_ends_at,
            "next_action_url": next_action_url,
            "radio_state": radio_state,
        })

    return templates.TemplateResponse(
        request=request,
        name="live_sessions.html",
        context={
            "app_name": settings.APP_NAME,
            "active_page": "sessions",
            "rows": rows,
            "music_tracks": music_tracks,
            "status_badge_class": STATUS_BADGE_CLASS,
            "success_message": SUCCESS_MESSAGES.get(success),
        },
    )


@app.post("/admin/quizzes/{quiz_id}/sessions/start")
def start_session(quiz_id: int, db: Session = Depends(get_db)):
    """PUBLISHED -> WAITING only. This must never touch
    current_question_index or current_question_started_at — opening the
    session for joining is a completely separate action from starting
    the quiz, even though both used to be combined in an earlier
    version of this app."""
    quiz = _get_quiz_or_404(db, quiz_id)
    if _session_phase(quiz, None) != "PUBLISHED":
        # already Live, or not yet Published — nothing to do
        return RedirectResponse(url="/admin/sessions", status_code=303)

    session = LiveSession(
        quiz_id=quiz.id,
        session_code=_unique_session_code(db),
        started_at=datetime.now(timezone.utc),
        status="Live",
        current_question_index=0,           # explicit: WAITING, not a question yet
        current_question_started_at=None,   # explicit: no timer running
    )
    quiz.status = "Live"
    db.add(session)
    db.commit()
    return RedirectResponse(url="/admin/sessions?success=session_started", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/start-quiz")
def start_quiz(quiz_id: int, db: Session = Depends(get_db)):
    """WAITING -> QUESTION_1, and only from there. This is the
    only place current_question_index ever becomes 1, and the only
    place a question timer ever starts running."""
    _lock_live_session_for_quiz_action(db, quiz_id)
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    if _session_phase(quiz, session) != "WAITING" or not quiz.questions:
        return RedirectResponse(url="/admin/sessions", status_code=303)

    session.current_question_index = 1
    session.current_question_started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)
    return RedirectResponse(url="/admin/sessions?success=quiz_started", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/next-question")
def next_question(
    quiz_id: int,
    request: Request,
    expected_question: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """QUESTION_N -> QUESTION_N+1, or QUESTION_N (the last one) ->
    QUIZ_COMPLETED. This is the single "move forward" action for both
    cases — advancing between real questions and wrapping up after the
    last one both go through here, which is what makes auto-advance
    naturally stop instead of ending the session: QUIZ_COMPLETED has no
    active question timer, so there's nothing left for auto-advance to
    fire on. Only valid from a real question phase. Also used by the
    auto-advance timer (see live_sessions.html), which calls this via
    fetch() and expects JSON back instead of a redirect.

    `expected_question` is the safety check against stale timers or
    duplicate browser tabs: the caller states which question it
    believes is currently active (the question number its own timer
    was counting down for). If the database's actual
    current_question_index has already moved on by the time this
    request is processed — e.g. another tab's timer already advanced
    it — this is a no-op instead of double-advancing. The manual
    "Next Question" button sends this too, so even a stale page reload
    can't skip a question; it just silently does nothing and the next
    page load shows the real state."""
    _lock_live_session_for_quiz_action(db, quiz_id)
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    total_questions = len(quiz.questions)
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    phase = _session_phase(quiz, session)

    if not phase.startswith("QUESTION_"):
        if is_fetch:
            return {"success": False, "reason": "not_on_a_question"}
        return RedirectResponse(url="/admin/sessions", status_code=303)

    if expected_question is not None and session.current_question_index != expected_question:
        # Someone else (another tab, an earlier stale timer) already
        # advanced past the question this request thought it was
        # ending. Do nothing rather than advance a second time.
        if is_fetch:
            return {
                "success": False,
                "reason": "stale",
                "current_question_index": session.current_question_index,
            }
        return RedirectResponse(url="/admin/sessions", status_code=303)

    session.current_question_index += 1
    if session.current_question_index <= total_questions:
        session.current_question_started_at = datetime.now(timezone.utc)
    else:
        session.current_question_started_at = None  # QUIZ_COMPLETED — no timer running

    db.commit()
    db.refresh(session)

    if is_fetch:
        return {"success": True}
    return RedirectResponse(url="/admin/sessions?success=next_question", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/finish")
def finish_quiz(
    quiz_id: int,
    request: Request,
    expected_question: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """QUIZ_COMPLETED -> SESSION_ENDED, and only from there. This is the only
    route that actually ends the session (sets status=Completed) — it
    can no longer be reached directly from a question phase, which was
    the actual bug: a single-question quiz (or any quiz on its last
    question) used to have no distinct "all done, review before ending"
    state, so finishing and ending were the same click. Now the teacher
    always passes through QUIZ_COMPLETED first, sees the "Finish
    Session" button and results, and this only fires when they
    explicitly choose to end it.

    `expected_question` guards against the same stale-tab/stale-timer
    problem as next-question: if provided and it doesn't match the
    question that was actually last active before completion, this is
    treated as stale and no-ops rather than force-ending the session.

    Sends the teacher to the Results Dashboard — unless this was called
    by the auto-advance timer via fetch(), which just wants a JSON
    acknowledgement so it can redirect the page itself."""
    _lock_live_session_for_quiz_action(db, quiz_id)
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    phase = _session_phase(quiz, session)

    if phase != "QUIZ_COMPLETED":
        # not yet through every question — e.g. still on QUESTION_N, or
        # WAITING, or already SESSION_ENDED
        if is_fetch:
            return {"success": False, "reason": "not_quiz_completed"}
        return RedirectResponse(url="/admin/sessions", status_code=303)

    if expected_question is not None and session.current_question_index != expected_question:
        # Session already moved (e.g. another tab's auto-advance beat
        # this one to QUIZ_COMPLETED at a different index) — stale, no-op.
        if is_fetch:
            return {
                "success": False,
                "reason": "stale",
                "current_question_index": session.current_question_index,
            }
        return RedirectResponse(url="/admin/sessions", status_code=303)

    session.status = "Completed"
    session.ended_at = datetime.now(timezone.utc)
    # Classroom Radio: same session-lifecycle-ends-the-radio policy as
    # the manual End Session route — see its comment for why this is
    # NOT triggered by an individual student finishing or a quiz pause.
    session.radio_status = "STOPPED"
    session.radio_position_seconds = 0.0
    session.radio_changed_at = datetime.now(timezone.utc)
    session.radio_revision += 1
    # The QUIZ reverts to Published (NOT "Completed") — this SESSION is
    # what just ended, not the quiz itself. A quiz that stayed
    # permanently "Completed" could never start a new session again
    # (start_session requires PUBLISHED), which was the actual bug: a
    # quiz became unusable forever after its first session ended. The
    # session's own row (status=Completed, ended_at, every Response
    # tied to session.id) is untouched by this — only the QUIZ's
    # availability for a future session changes.
    quiz.status = "Published"
    db.commit()

    if is_fetch:
        return {"success": True}
    return RedirectResponse(url=f"/admin/results?session_id={session.id}", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/end")
def end_session(quiz_id: int, db: Session = Depends(get_db)):
    _lock_live_session_for_quiz_action(db, quiz_id)
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    if session:
        session.status = "Completed"
        session.ended_at = datetime.now(timezone.utc)
        # Classroom Radio: only the SESSION lifecycle stops the radio —
        # never an individual student finishing, never a quiz pause.
        # The session itself is what's ending here, so the radio must
        # not keep reporting PLAYING against a session no student can
        # still be connected to.
        session.radio_status = "STOPPED"
        session.radio_position_seconds = 0.0
        session.radio_changed_at = datetime.now(timezone.utc)
        session.radio_revision += 1
    # See the identical comment in finish_quiz above — reverting the
    # quiz to Published (not Completed) is what makes it reusable for a
    # later session, without touching this session's own historical record.
    quiz.status = "Published"
    db.commit()
    return RedirectResponse(url="/admin/sessions?success=session_ended", status_code=303)



# ---------------------------------------------------------- classroom radio: controls --
# Every route here is a teacher-only control (protected by the existing
# /admin auth middleware — students have no path to any of these) that
# follows the same shape: load the session, optionally reject a stale
# request via expected_revision (mirroring next-question/finish's exact
# idempotency pattern), freeze the current effective position, apply
# this action's specific change, bump the revision, commit once.

def _radio_reject_stale(session: LiveSession, expected_revision: int | None, is_fetch: bool):
    """Shared stale-request guard for every radio control action — a
    delayed duplicate of an earlier click (e.g. a slow double-tap of
    Play, or a request that arrives after a newer one already changed
    the state) must not silently re-apply on top of a NEWER state.
    Returns a response dict/redirect if the request should be rejected
    as stale, or None if it's fine to proceed."""
    if expected_revision is not None and session.radio_revision != expected_revision:
        if is_fetch:
            return {"success": False, "reason": "stale", "revision": session.radio_revision}
        return RedirectResponse(url="/admin/sessions", status_code=303)
    return None


@app.post("/admin/quizzes/{quiz_id}/sessions/radio-select")
def radio_select(
    quiz_id: int, request: Request, track_id: int = Form(...), db: Session = Depends(get_db)
):
    """Selecting a track is always a clean start: STOPPED at position 0
    on the newly selected track, regardless of what the previous track
    was doing — continuing the OLD track's elapsed-time math against a
    DIFFERENT track's timeline would be meaningless."""
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    if not session:
        return {"success": False, "reason": "no_session"} if is_fetch else RedirectResponse(url="/admin/sessions", status_code=303)

    track = db.query(MusicTrack).filter(MusicTrack.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    session.radio_track_id = track.id
    session.radio_status = "STOPPED"
    session.radio_position_seconds = 0.0
    session.radio_changed_at = datetime.now(timezone.utc)
    session.radio_revision += 1
    db.commit()

    if is_fetch:
        return {"success": True, **_radio_effective_state(session)}
    return RedirectResponse(url="/admin/sessions", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/radio-play")
def radio_play(
    quiz_id: int, request: Request, expected_revision: int | None = Form(None), db: Session = Depends(get_db)
):
    """PLAY (covers both initial play and Resume-from-pause — there's no
    separate 'resume' action because the behavior is identical: continue
    from wherever radio_position_seconds currently is). The one special
    case is a track that has already reached its end — playing from
    exactly the end would just immediately stop again, so that specific
    case restarts from 0 instead."""
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    if not session or not session.radio_track_id:
        return {"success": False, "reason": "no_track_selected"} if is_fetch else RedirectResponse(url="/admin/sessions", status_code=303)

    stale = _radio_reject_stale(session, expected_revision, is_fetch)
    if stale is not None:
        return stale

    _radio_freeze(session)
    track_duration = session.radio_track.duration_seconds
    if track_duration is not None and session.radio_position_seconds >= track_duration:
        session.radio_position_seconds = 0.0
    session.radio_status = "PLAYING"
    session.radio_changed_at = datetime.now(timezone.utc)
    session.radio_revision += 1
    db.commit()

    if is_fetch:
        return {"success": True, **_radio_effective_state(session)}
    return RedirectResponse(url="/admin/sessions", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/radio-pause")
def radio_pause(
    quiz_id: int, request: Request, expected_revision: int | None = Form(None), db: Session = Depends(get_db)
):
    """PAUSE — freezes at the current effective position, exactly as-is
    (no rounding beyond _radio_effective_state's own), so Resume
    continues from precisely here rather than from zero."""
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    if not session:
        return {"success": False, "reason": "no_session"} if is_fetch else RedirectResponse(url="/admin/sessions", status_code=303)

    stale = _radio_reject_stale(session, expected_revision, is_fetch)
    if stale is not None:
        return stale

    _radio_freeze(session)
    session.radio_status = "PAUSED"
    session.radio_changed_at = datetime.now(timezone.utc)
    session.radio_revision += 1
    db.commit()

    if is_fetch:
        return {"success": True, **_radio_effective_state(session)}
    return RedirectResponse(url="/admin/sessions", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/radio-stop")
def radio_stop(
    quiz_id: int, request: Request, expected_revision: int | None = Form(None), db: Session = Depends(get_db)
):
    """STOP -> position becomes 0 (the documented, deterministic
    behavior — see PHASE_2_CLASSROOM_RADIO.md). Unlike Pause, Stop does
    NOT preserve position; a subsequent Play starts fresh from 0."""
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    if not session:
        return {"success": False, "reason": "no_session"} if is_fetch else RedirectResponse(url="/admin/sessions", status_code=303)

    stale = _radio_reject_stale(session, expected_revision, is_fetch)
    if stale is not None:
        return stale

    session.radio_status = "STOPPED"
    session.radio_position_seconds = 0.0
    session.radio_changed_at = datetime.now(timezone.utc)
    session.radio_revision += 1
    db.commit()

    if is_fetch:
        return {"success": True, **_radio_effective_state(session)}
    return RedirectResponse(url="/admin/sessions", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/radio-reset")
def radio_reset(
    quiz_id: int, request: Request, expected_revision: int | None = Form(None), db: Session = Depends(get_db)
):
    """RESET -> position becomes 0, but UNLIKE Stop, the current
    PLAYING/PAUSED status is preserved: resetting while playing
    immediately restarts the track from 0 for everyone; resetting while
    paused moves everyone to 0 and stays paused there (documented in
    PHASE_2_CLASSROOM_RADIO.md, matching the spec's recommended
    behavior)."""
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    if not session:
        return {"success": False, "reason": "no_session"} if is_fetch else RedirectResponse(url="/admin/sessions", status_code=303)

    stale = _radio_reject_stale(session, expected_revision, is_fetch)
    if stale is not None:
        return stale

    effective = _radio_freeze(session)  # determines whether we're currently PLAYING, PAUSED, or STOPPED
    session.radio_position_seconds = 0.0
    session.radio_status = effective["status"]  # preserved, not forced to STOPPED
    session.radio_changed_at = datetime.now(timezone.utc)
    session.radio_revision += 1
    db.commit()

    if is_fetch:
        return {"success": True, **_radio_effective_state(session)}
    return RedirectResponse(url="/admin/sessions", status_code=303)


@app.post("/admin/quizzes/{quiz_id}/sessions/radio-seek")
def radio_seek(
    quiz_id: int, request: Request,
    delta_seconds: float = Form(...),
    expected_revision: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """SEEK by a relative delta (+10 / -10, or any value) — clamped to
    never go below 0, and clamped to the track's own duration when
    known (WAV only; an unknown-duration track can seek arbitrarily
    forward, which the browser's own <audio> element clamps to its real
    length automatically on the student side). Status (PLAYING/PAUSED)
    is preserved — seeking doesn't start or stop playback, just moves
    the position."""
    quiz = _get_quiz_or_404(db, quiz_id)
    session = _get_current_session(db, quiz.id)
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    if not session or not session.radio_track_id:
        return {"success": False, "reason": "no_track_selected"} if is_fetch else RedirectResponse(url="/admin/sessions", status_code=303)

    stale = _radio_reject_stale(session, expected_revision, is_fetch)
    if stale is not None:
        return stale

    effective = _radio_freeze(session)
    new_position = effective["position"] + delta_seconds
    new_position = max(new_position, 0.0)
    track_duration = session.radio_track.duration_seconds
    if track_duration is not None:
        new_position = min(new_position, track_duration)
    session.radio_position_seconds = new_position
    session.radio_status = effective["status"]  # unchanged by seeking
    session.radio_changed_at = datetime.now(timezone.utc)
    session.radio_revision += 1
    db.commit()

    if is_fetch:
        return {"success": True, **_radio_effective_state(session)}
    return RedirectResponse(url="/admin/sessions", status_code=303)


@app.get("/admin/sessions/{session_id}/qrcode")
def session_qrcode(session_id: int, request: Request, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    host = request.url.hostname or "127.0.0.1"
    port = f":{request.url.port}" if request.url.port else ""
    join_url = f"{request.url.scheme}://{host}{port}/join/{session.session_code}"

    img = qrcode.make(join_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
