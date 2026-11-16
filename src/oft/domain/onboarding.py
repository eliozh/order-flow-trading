"""Canonical onboarding step identity (F01, slice 44).

One definition is shared by the application service's dataclass and the Pydantic
response model so a renamed or added step cannot drift between them.
"""

from __future__ import annotations

from typing import Literal

OnboardingStepId = Literal["environment", "llm", "binance", "telegram", "watchlist"]

ONBOARDING_STEP_IDS: tuple[OnboardingStepId, ...] = (
    "environment",
    "llm",
    "binance",
    "telegram",
    "watchlist",
)

__all__ = ["ONBOARDING_STEP_IDS", "OnboardingStepId"]
