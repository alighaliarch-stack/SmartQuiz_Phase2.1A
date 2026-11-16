from __future__ import annotations

from datetime import datetime, timedelta, timezone

RECALL_RATINGS = ("Again", "Hard", "Good", "Easy")


class SpacedRepetitionScheduler:
    """Deterministic, review-state-based scheduler for flashcards.

    The initial algorithm intentionally stays simple and configurable so it can be
    replaced later by FSRS or another scheduler without changing the student UI.
    It keeps a small amount of state for each student/flashcard pair and updates the
    next review date based on the rating chosen after revealing the answer.
    """

    @staticmethod
    def normalise_rating(rating: str) -> str:
        if rating is None:
            raise ValueError("Recall rating is required.")
        candidate = str(rating).strip().title()
        if candidate not in RECALL_RATINGS:
            raise ValueError(f"Unsupported recall rating: {rating!r}")
        return candidate

    @classmethod
    def calculate_next_review(
        cls,
        rating: str,
        current_interval_days: int = 0,
        ease_factor: float = 2.5,
        now: datetime | None = None,
    ) -> dict:
        rating = cls.normalise_rating(rating)
        now = now or datetime.now(timezone.utc)

        if rating == "Again":
            interval_days = max(1, int(current_interval_days / 2) if current_interval_days else 1)
            ease_factor = max(1.3, ease_factor - 0.2)
        elif rating == "Hard":
            interval_days = max(1, current_interval_days or 1)
            ease_factor = max(1.3, ease_factor - 0.05)
        elif rating == "Good":
            interval_days = max(2, current_interval_days + (current_interval_days or 2))
            ease_factor = ease_factor + 0.05
        elif rating == "Easy":
            interval_days = max(3, (current_interval_days or 2) + 3)
            ease_factor = ease_factor + 0.15
        else:
            raise ValueError(f"Unsupported recall rating: {rating!r}")

        return {
            "rating": rating,
            "interval_days": interval_days,
            "ease_factor": round(ease_factor, 2),
            "next_due_at": now + timedelta(days=interval_days),
        }
