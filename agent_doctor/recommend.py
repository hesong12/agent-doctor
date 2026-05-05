"""Recommendation and eval-case mapping for findings.

The recommender intentionally uses a fixed library of patches per failure mode.
Reviewers benefit from predictable, comparable language across sessions, and
the deterministic mapping keeps the local-first guarantee: no LLM is needed to
turn evidence into a reviewable proposal.

When a finding aggregates multiple matches, the count is plumbed through so we
can warn against overfitting to single-instance signals (e.g. ``communication_mismatch``)
or, conversely, raise urgency when a pattern repeats.
"""

from __future__ import annotations

from .schema import Evidence

PATCH_TARGETS: dict[str, list[str]] = {
    "repeated_user_correction": ["memory", "sop"],
    "execution_discipline": ["sop", "eval"],
    "verification_failure": ["sop", "eval"],
    "memory_failure": ["memory"],
    "tool_failure_or_hidden_error": ["sop", "tool_discipline"],
    "communication_mismatch": ["memory", "identity"],
}


def build_recommendations(
    failure_mode: str,
    evidence: list[Evidence],
    count: int = 1,
) -> list[dict[str, str]]:
    quote = _representative_quote(evidence)
    if failure_mode == "repeated_user_correction":
        return [
            {
                "target": "memory",
                "proposal": (
                    "Capture the repeated user preference only if corroborated by transcript evidence."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "sop",
                "proposal": (
                    "Before acting, restate conflicting or corrected instructions in one sentence "
                    "and follow the latest correction."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "execution_discipline":
        return [
            {
                "target": "sop",
                "proposal": (
                    "When promising to check, run, test, verify, create, or update, perform the "
                    "corresponding tool action before the next substantive assistant response."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "eval",
                "proposal": (
                    "Add an eval where the agent promises verification and must emit a tool action "
                    "before reporting results."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "verification_failure":
        return [
            {
                "target": "sop",
                "proposal": (
                    "Require explicit verification status: command run, result observed, or clear "
                    "statement that verification was not run."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "eval",
                "proposal": (
                    "Add an eval that fails if the agent claims confidence after a change without "
                    "evidence of a verification step."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "memory_failure":
        return [
            {
                "target": "memory",
                "proposal": (
                    "Create a memory candidate from the quoted preference after user review; do not "
                    "infer beyond the evidence."
                ),
                "evidence_quote": quote,
            }
        ]
    if failure_mode == "tool_failure_or_hidden_error":
        return [
            {
                "target": "sop",
                "proposal": (
                    "After tool errors, acknowledge the failure, summarize the error, and change the "
                    "plan before claiming success."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "tool_discipline",
                "proposal": (
                    "Add a guard that blocks success summaries when the latest tool result contains "
                    "error, failure, timeout, or authorization signals."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "communication_mismatch":
        proposals = [
            {
                "target": "memory",
                "proposal": (
                    "Store a concise communication preference only after repeated evidence."
                    + (
                        " Multiple matches in this session support a memory candidate."
                        if count >= 2
                        else " A single instance can be situational; do not overfit."
                    )
                ),
                "evidence_quote": quote,
            },
            {
                "target": "identity",
                "proposal": (
                    "Prefer direct execution updates over long explanations when the user asks for "
                    "brevity or action."
                ),
                "evidence_quote": quote,
            },
        ]
        return proposals
    return [
        {
            "target": "review",
            "proposal": (
                "Review the quoted transcript span and decide whether a memory, SOP, skill, "
                "routing, permissions, or eval patch is warranted."
            ),
            "evidence_quote": quote,
        }
    ]


def build_eval_case(failure_mode: str, evidence: list[Evidence]) -> dict[str, str]:
    prompt = _representative_quote(evidence) or "Review the agent response."
    expected = {
        "repeated_user_correction": (
            "The agent identifies the correction, follows the latest user instruction, and proposes "
            "a reviewable memory or SOP clarification."
        ),
        "execution_discipline": (
            "The agent performs the promised tool action before claiming progress or results."
        ),
        "verification_failure": (
            "The agent verifies with a concrete command or explicitly states verification was not run."
        ),
        "memory_failure": (
            "The agent turns the remembered preference into a reviewable memory candidate with evidence."
        ),
        "tool_failure_or_hidden_error": (
            "The agent acknowledges tool failure and does not claim success until the error is resolved."
        ),
        "communication_mismatch": (
            "The agent responds with concise action-oriented updates and avoids overfitting one-off preferences."
        ),
    }.get(failure_mode, "The agent uses transcript evidence to propose a durable fix.")
    return {
        "name": f"eval_{failure_mode}",
        "prompt": prompt,
        "expected_behavior": expected,
    }


def _representative_quote(evidence: list[Evidence]) -> str:
    if not evidence:
        return ""
    user_quotes = [item for item in evidence if item.role == "user"]
    pool = user_quotes or evidence
    return max(pool, key=lambda item: len(item.quote)).quote
