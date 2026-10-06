"""
ai_grading.py
--------------
Level-3 semantic evaluator for Short Answer grading — only invoked when
deterministic matching (exact/keyword/concept-coverage, see
_grade_short_answer in app.py) can't confidently classify an answer.

This is a provider-agnostic abstraction: the grading pipeline in app.py
only ever calls evaluate_semantic_match() and never knows or cares which
underlying provider actually ran. A different backend can be substituted
by adding another branch here without touching app.py at all.

Configuration — all via environment variables, never hard-coded:
    AI_GRADING_PROVIDER    "gemini" (default), "anthropic", or "mock"
                            (tests only)
    GEMINI_API_KEY          required for the "gemini" provider
    GEMINI_MODEL            Gemini model name for the "gemini" provider,
                            e.g. "gemini-2.5-flash" (default) — override
                            to switch models without touching code
    ANTHROPIC_API_KEY       required for the "anthropic" provider
    AI_GRADING_MODEL        model name for the "anthropic" provider
    AI_GRADING_MOCK_RESPONSE   JSON string, only read by the "mock"
                            provider — lets the grading pipeline be
                            exercised deterministically in tests
                            without any network access or real key.
                            Never selected unless AI_GRADING_PROVIDER
                            is explicitly set to "mock".

If no provider is configured, or the call fails/times out/errors for
any reason, this raises AIEvaluationUnavailable. Callers (see
_grade_short_answer / _run_ai_grading_task in app.py) must catch it and
fall back to NEEDS_REVIEW — never let it propagate into a crash, and
never treat it as an ordinary INCORRECT.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

# See the identical call and comment in app.py — this one is a defensive
# duplicate so this module correctly picks up GEMINI_API_KEY from a .env
# file even if it's ever imported/run standalone, without depending on
# app.py having already done it. load_dotenv() is safe to call more than
# once (idempotent, does not override an already-set real env var).
load_dotenv()

ALLOWED_DECISIONS = {"CORRECT", "INCORRECT", "NEEDS_REVIEW"}

# Kept modest and explicit: the grading pipeline is only ever asked to
# classify one short answer against one question, not to do open-ended
# reasoning, so a short timeout keeps a slow/unreachable provider from
# holding the background grading task open indefinitely.
REQUEST_TIMEOUT_SECONDS = 20


class AIEvaluationUnavailable(Exception):
    """Semantic evaluation could not be performed right now — no
    provider configured, a network/timeout/HTTP error, or a response
    that didn't match the required structured-JSON shape. This is a
    normal, expected condition (e.g. no API key configured in this
    environment), not a bug — callers must treat it as NEEDS_REVIEW."""


def evaluate_semantic_match(
    question_text: str,
    expected_answer: str,
    accepted_answers: list[str],
    student_answer: str,
    points: int,
    grading_notes: str = "",
) -> dict:
    """Returns a dict with the REQUIRED keys:
        "decision":   "CORRECT" | "INCORRECT" | "NEEDS_REVIEW"
        "confidence": float in [0, 1]
        "reason":     str, a short one-sentence explanation
    and these OPTIONAL keys (present when the provider returns them;
    callers must not assume they exist — see app.py's _compose_ai_reason):
        "matched_concepts":         list[str]
        "missing_optional_concepts": list[str]
        "contradictions":           list[str]

    grading_notes is optional free-text guidance from the teacher
    (required concepts, accepted synonyms, anything that should or
    shouldn't count) — folded into the prompt as extra context when
    present; the AI is explicitly told it's guidance, not the only
    source of truth, so it doesn't override its own judgment of the
    expected answer.

    Raises AIEvaluationUnavailable rather than returning malformed or
    free-form data — the caller can rely on getting back a dict with at
    least the three required keys, or an exception, never anything
    silently wrong in between.
    """
    provider = os.environ.get("AI_GRADING_PROVIDER", "gemini")
    if provider == "gemini":
        return _evaluate_via_gemini(question_text, expected_answer, accepted_answers, student_answer, points, grading_notes)
    if provider == "anthropic":
        return _evaluate_via_anthropic(question_text, expected_answer, accepted_answers, student_answer, points, grading_notes)
    if provider == "mock":
        return _evaluate_via_mock()
    raise AIEvaluationUnavailable(f"Unknown AI_GRADING_PROVIDER: {provider!r}")


def _build_prompt(question_text: str, expected_answer: str, accepted_answers: list[str],
                   student_answer: str, points: int, grading_notes: str) -> str:
    accepted_block = "\n".join(f"- {a}" for a in accepted_answers) if accepted_answers else "(none provided)"
    notes_block = (
        f"\nTeacher's grading guidance (helpful context, not the only source of truth — "
        f"use it alongside your own judgment of the expected answer):\n{grading_notes}\n"
        if grading_notes.strip() else ""
    )
    return f"""You are grading a short-answer exam question. The student does NOT need to use the same words or sentence structure as the expected answer — judge MEANING, not wording similarity. Be conservative about awarding credit, but also fair: do not penalize valid paraphrasing, synonyms, reordering, or additional correct information.

Distinguish between:
- REQUIRED core concepts: the essential definitional content of the expected answer. All of these must be substantively present (in the student's own words is fine) for a CORRECT grade.
- OPTIONAL supporting details / examples: illustrative examples or extra specifics in the expected answer (e.g. things introduced by "such as", "like", "for example"). Omitting some of these does NOT make an answer incorrect, as long as the required concepts are present.

Evaluate:
1. Does the answer actually address the question?
2. Are the required core concepts present (possibly in different words)?
3. Is the core meaning preserved, even if phrased differently (synonyms, paraphrasing, reordering)?
4. Does it contradict the expected answer (states the opposite or a materially wrong claim)?
5. Is it missing an important required concept (not just missing an optional example)?
6. Does it merely contain related/expected keywords without actually expressing the required meaning (a "keyword salad")? This must NOT be marked CORRECT.
7. Does it include additional valid information beyond the expected answer? That is fine and should not count against it, unless the addition contradicts the expected concept.

Do not award credit for an answer that merely sounds plausible or scatters expected keywords without actually answering the question. If you are genuinely uncertain after considering the above, respond NEEDS_REVIEW rather than guessing.

Question: {question_text}
Points available: {points}
Expected answer: {expected_answer}
Other accepted phrasings:
{accepted_block}
{notes_block}
Student's answer: {student_answer}

Respond with ONLY a JSON object, no other text, in exactly this shape:
{{"decision": "CORRECT" | "INCORRECT" | "NEEDS_REVIEW", "confidence": <number between 0.0 and 1.0>, "matched_concepts": [<short phrases for each required concept the student's answer actually covers>], "missing_optional_concepts": [<short phrases for expected examples/details the student omitted, if any — these should NOT by themselves cause INCORRECT>], "contradictions": [<short phrases describing anything the student's answer states that contradicts the expected answer, if any>], "reason": "<one short sentence, no more than 25 words, explaining the decision>"}}"""


def _redact_key(text: str, api_key: str) -> str:
    """Defensive scrub: if the raw API key ever ended up inside an
    exception message for any reason, it must never reach a log line,
    the stored evaluation_reason, or anything else downstream. Verified
    empirically that the SDK's own errors don't include it, but this
    costs nothing and removes any doubt."""
    if api_key and api_key in text:
        return text.replace(api_key, "[redacted]")
    return text


def _evaluate_via_gemini(question_text: str, expected_answer: str, accepted_answers: list[str],
                          student_answer: str, points: int, grading_notes: str) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise AIEvaluationUnavailable("GEMINI_API_KEY is not configured")

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    prompt = _build_prompt(question_text, expected_answer, accepted_answers, student_answer, points, grading_notes)

    try:
        from google import genai
        from google.genai import types
        from google.genai import errors as genai_errors
    except ImportError as exc:
        raise AIEvaluationUnavailable(f"google-genai package not installed: {exc}") from exc

    try:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000),
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",  # native structured-output mode, not just a prompt instruction
                temperature=0.0,  # deterministic-as-possible grading, not creative variation
            ),
        )
    except genai_errors.APIError as exc:
        # Covers auth errors, rate limits (429), quota exhaustion, 5xx
        # server errors, and the "host not in allowlist" network-policy
        # error this sandbox itself produces — all surfaced by the SDK
        # as this one exception type, confirmed empirically.
        raise AIEvaluationUnavailable(_redact_key(f"Gemini request failed: {exc}", api_key)) from exc
    except Exception as exc:
        # Broad catch as a last-resort safety net for anything the SDK's
        # underlying HTTP client raises that isn't wrapped as APIError
        # (e.g. a raw connection/timeout error) — this function's only
        # job is "get a grading decision or explain why not", so nothing
        # from it may ever propagate into a live quiz as a crash.
        raise AIEvaluationUnavailable(_redact_key(f"Gemini request failed: {exc}", api_key)) from exc

    try:
        text = response.text
        if not text:
            raise ValueError("empty response text")
        parsed = json.loads(text)
        decision = parsed["decision"]
        confidence = float(parsed["confidence"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, AttributeError) as exc:
        raise AIEvaluationUnavailable(f"Malformed Gemini response: {exc}") from exc

    if decision not in ALLOWED_DECISIONS or not (0.0 <= confidence <= 1.0):
        raise AIEvaluationUnavailable(f"Gemini returned an invalid decision/confidence: {parsed!r}")

    return _extract_result_fields(parsed, decision, confidence)


def _evaluate_via_anthropic(question_text: str, expected_answer: str, accepted_answers: list[str],
                             student_answer: str, points: int, grading_notes: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AIEvaluationUnavailable("ANTHROPIC_API_KEY is not configured")

    model = os.environ.get("AI_GRADING_MODEL", "claude-3-5-haiku-20241022")
    prompt = _build_prompt(question_text, expected_answer, accepted_answers, student_answer, points, grading_notes)
    body = json.dumps({
        "model": model,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIEvaluationUnavailable(f"AI request failed: {exc}") from exc

    try:
        text = payload["content"][0]["text"]
        parsed = json.loads(text)
        decision = parsed["decision"]
        confidence = float(parsed["confidence"])
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AIEvaluationUnavailable(f"Malformed AI response: {exc}") from exc

    if decision not in ALLOWED_DECISIONS or not (0.0 <= confidence <= 1.0):
        raise AIEvaluationUnavailable(f"AI returned an invalid decision/confidence: {parsed!r}")

    return _extract_result_fields(parsed, decision, confidence)


def _extract_result_fields(parsed: dict, decision: str, confidence: float) -> dict:
    """Pulls the required fields plus the optional concept-list fields
    out of a parsed AI response, defensively — a malformed or
    unexpected-shaped optional field (e.g. a string instead of a list)
    is just dropped rather than raising, since these fields are
    supplementary explanation, not required for a valid decision.

    missing_concepts is accepted as an alias for missing_optional_concepts
    (some prompts/providers may use either name) — both map to the same
    internal key so app.py's consuming code never needs to know which
    one a given provider happened to return.
    """
    def _string_list(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v) for v in value if isinstance(v, (str, int, float))]

    missing = parsed.get("missing_optional_concepts")
    if missing is None:
        missing = parsed.get("missing_concepts")

    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(parsed.get("reason", "")),
        "matched_concepts": _string_list(parsed.get("matched_concepts")),
        "missing_optional_concepts": _string_list(missing),
        "contradictions": _string_list(parsed.get("contradictions")),
    }


def _evaluate_via_mock() -> dict:
    """Test-only provider. Reads a canned JSON response from the
    AI_GRADING_MOCK_RESPONSE environment variable so the grading
    pipeline's Level-3 branch (including the failure path) can be
    exercised deterministically without any real network access.
    Never selected in normal operation — only when a test explicitly
    sets AI_GRADING_PROVIDER=mock."""
    raw = os.environ.get("AI_GRADING_MOCK_RESPONSE")
    if not raw or raw == "FAIL":
        raise AIEvaluationUnavailable("mock provider configured to simulate a failure")
    try:
        parsed = json.loads(raw)
        decision = parsed["decision"]
        confidence = float(parsed["confidence"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AIEvaluationUnavailable(f"Malformed AI_GRADING_MOCK_RESPONSE: {exc}") from exc
    if decision not in ALLOWED_DECISIONS or not (0.0 <= confidence <= 1.0):
        raise AIEvaluationUnavailable(f"Invalid mock decision/confidence: {parsed!r}")
    return _extract_result_fields(parsed, decision, confidence)
