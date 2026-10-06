# SmartQuiz — Phase 1 Baseline

## Purpose

SmartQuiz is a locally-hosted, live classroom quiz platform. A teacher creates a quiz (Multiple Choice single/multiple, True/False, Short Answer), starts a live session, and students join from their own devices using a 6-character code, a join link, or a QR code. Questions are pushed to every connected student in sync, each on its own countdown timer; answers are graded automatically (deterministically for MCQ/True-False, and via a hybrid deterministic + AI semantic pipeline for Short Answer); results and a live leaderboard update as answers come in.

This document describes the **Phase 1 baseline**: individual per-question timers, proportional multiple-answer MCQ scoring, teacher authentication, student result privacy, and race-condition hardening, on top of the working MVP that preceded it.

---

## Requirements

- **Python**: 3.10 or newer (the codebase uses `X | None` union type-hint syntax throughout, which requires 3.10+).
- **OS**: Any platform Python and SQLite run on (Windows, macOS, Linux). No OS-specific code.

## Installation

```bash
pip install -r requirements.txt
```

Dependencies (from `requirements.txt`):

| Package | Purpose |
|---|---|
| `fastapi` | Web framework |
| `uvicorn[standard]` | ASGI server |
| `jinja2` | HTML templating |
| `sqlalchemy` | ORM / database access |
| `python-multipart` | Form/file upload parsing |
| `qrcode[pil]` | QR code generation for join links |
| `google-genai` | Official Gemini SDK, used for Short Answer semantic grading |
| `python-dotenv` | Loads `.env` into the process environment at startup |
| `itsdangerous` | Signs the teacher login session cookie (via Starlette's `SessionMiddleware`) |

No other installation step is required — the SQLite database file and all tables are created automatically the first time the app starts.

---

## Environment variables

Copy `.env.example` to `.env` and fill in what you need:

```bash
cp .env.example .env
```

| Variable | Required? | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | Only for AI-assisted Short Answer grading | Your Gemini API key. Short Answer grading still works without it — answers that need semantic judgment fall back to `NEEDS_REVIEW` for the teacher to resolve manually, rather than failing. |
| `GEMINI_MODEL` | No (defaults to `gemini-2.5-flash`) | Override to use a different Gemini model. |
| `AI_GRADING_PROVIDER` | No (defaults to `gemini`) | Can be set to `anthropic` (with `ANTHROPIC_API_KEY`) as an alternative provider, or `mock` for testing. |
| `TEACHER_INITIAL_PASSWORD` | No | Sets the teacher password the *first* time the app ever starts. If left unset, a random password is generated and printed once to the server console — see below. Has no effect after the first startup; the real password lives in the database and is changed from Settings. |
| `SESSION_SECRET_KEY` | No | Signs the login session cookie. If unset, a random key is generated every time the server starts, which just means everyone gets signed out on a restart (a safe default). Set a fixed value if you want logins to survive restarts. |

**No real secrets are included in this baseline package.** `.env.example` contains only placeholders and comments; the actual `.env` you create yourself is git-ignored (see `.gitignore`) and was never included in this ZIP.

---

## Starting the server

```bash
uvicorn app:app --reload
```

This starts the server at **http://127.0.0.1:8000** by default (uvicorn's own default host/port).

The very first time it starts, it will print either:
- confirmation that the teacher password was bootstrapped from `TEACHER_INITIAL_PASSWORD`, or
- a **temporary generated password**, printed once to the console, if you didn't set one.

Copy that password before you close the terminal — it isn't shown again, though you can always reset it by clearing the `app_settings` table's `teacher_password_hash` row if needed (or just log in with whatever the console showed).

---

## Teacher login procedure

1. Go to `/login` (or click "Teacher Dashboard" from the homepage).
2. Enter the password (from your `.env`, or the one printed to the console on first startup).
3. You're redirected to `/admin`. From there: create a quiz, add questions (each with its own points, duration, and correct answer(s)), publish it, then go to Live Sessions to start a session.
4. To change your password, go to Settings (in the sidebar) once logged in.
5. Log out via the "Log Out" link in the sidebar.

All `/admin/*` routes require an authenticated session — visiting any of them without logging in redirects to `/login`.

## Student joining procedure

1. A student goes to `/join` (or scans the QR code / follows the join link the teacher shares from Live Sessions).
2. They enter their name and roll number (course/section optional) and submit.
3. They land in a waiting room until the teacher starts the quiz, then see each question in turn with its own countdown, synced to the server.
4. After the quiz, each student sees a "View My Results" link — this is a private link containing an unguessable token; it shows only that student's own score and breakdown, never anyone else's.

---

## Database initialization / migration behavior

The SQLite database file (`smartquiz.db`, created next to `app.py` — not included in this ZIP) is created automatically on first startup via `init_db()`. Every subsequent startup runs a small set of idempotent migrations that add any new columns/tables an older database file doesn't have yet, without deleting or resetting existing data. This includes, as of Phase 1:

- Adding `duration_seconds` to existing questions, backfilled from each quiz's own prior duration (not a generic default).
- Adding `result_token` to existing students, so no one already in the database loses access to their own results.
- Creating the `app_settings` table.

If you're starting completely fresh, none of this matters — the database is simply created with the current schema from the start.

---

## Local development URL

**http://127.0.0.1:8000** (or whatever port you pass to `uvicorn --port`).

## LAN usage

The application already supports this without any code change: join links and QR codes are generated dynamically from whatever host the *teacher's own browser* is currently using to reach the app (`request.url.hostname`) — not hardcoded to `127.0.0.1`. This means if you start the server bound to all interfaces —

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

— and the teacher opens the dashboard using the machine's LAN IP address (e.g. `http://192.168.1.50:8000/admin`) rather than `127.0.0.1`, the generated join links and QR codes will automatically use that same LAN IP, so other devices on the same network can reach `/join` directly.

**What this document does not claim**: whether a specific firewall rule, router configuration, or a particular phone/OS has actually been tested reaching the server is environment-specific and outside what this codebase itself controls. Confirm connectivity in your own network before relying on it in a live classroom.

---

## Security notes

- Passwords are never stored in plaintext — only a PBKDF2-HMAC-SHA256 hash, generated with a random per-password salt.
- The password hash lives in the database, not in an environment variable, so it can be changed at runtime without editing `.env` or restarting the server.
- `/admin/*` routes are protected **server-side**, at the HTTP middleware layer — not by hiding buttons/links in the UI. A student manually typing an admin URL is redirected to `/login`, not shown a broken page.
- Student result pages require an unguessable token (not just the sequential student ID) to view — swapping the ID in the URL, or guessing a token, both fail with an identical error, so the failure itself can't be used to find out which student IDs are real.
- `.env` is listed in `.gitignore` and must never be committed. Only `.env.example` (placeholders/comments, no real values) is meant to be shared or version-controlled.
- If you expose this server beyond `127.0.0.1` (e.g. binding to `0.0.0.0` for LAN access), be aware that anyone who can reach that port on the network can attempt to log in as the teacher — use a real, non-trivial password, and consider closing that access outside of active class time.
