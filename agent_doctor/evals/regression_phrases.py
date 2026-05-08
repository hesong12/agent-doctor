"""Regression phrase library for user-frustration detection.

This module records phrases that *must* trip the local
``classify_user_frustration`` classifier as a high-severity signal. The list
is intentionally small and curated: each entry corresponds to a real-world
miss that prompted improvement of the detector.

The bench harness and unit tests load this list directly so that a future
detector change cannot silently drop coverage on these phrases. Anyone
editing :mod:`agent_doctor.frustration` should run the regression test (see
``tests/test_evals.py::test_regression_phrases_are_high_severity``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegressionPhrase:
    """A phrase that must remain a high-severity frustration signal."""

    text: str
    rationale: str
    expected_labels: tuple[str, ...]


REGRESSION_PHRASES: tuple[RegressionPhrase, ...] = (
    RegressionPhrase(
        text="你最近怎么越来越笨了",
        rationale=(
            "Cumulative trust-degradation phrasing observed in production. The "
            "previous detector matched ``你这么笨`` but did not catch the "
            "``越来越笨`` shape that signals worsening over time."
        ),
        expected_labels=("trust_degradation",),
    ),
    RegressionPhrase(
        text="越来越蠢",
        rationale="Trust-degradation variant using 蠢 instead of 笨.",
        expected_labels=("trust_degradation",),
    ),
    RegressionPhrase(
        text="你越来越没用",
        rationale="Trust-degradation phrased as cumulative uselessness.",
        expected_labels=("trust_degradation",),
    ),
    RegressionPhrase(
        text="You are getting worse and worse.",
        rationale="English equivalent of 越来越笨; cumulative trust loss.",
        expected_labels=("trust_degradation",),
    ),
    RegressionPhrase(
        text="lately you seem dumber",
        rationale="English trust-degradation phrasing.",
        expected_labels=("trust_degradation",),
    ),
    RegressionPhrase(
        text="你怎么这么笨的？",
        rationale="Direct quality complaint that must remain high severity.",
        expected_labels=("direct_quality_complaint",),
    ),
)
