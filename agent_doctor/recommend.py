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
    "user_frustration_signal": ["identity", "sop", "eval"],
    "trust_degradation_episode": ["identity", "sop", "eval"],
    "missed_core_question": ["sop", "eval"],
    "instruction_drift": ["sop", "eval"],
    "over_process_response": ["identity", "sop"],
    "unsupported_completion_claim": ["sop", "eval"],
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
    if failure_mode == "user_frustration_signal":
        return [
            {
                "target": "identity",
                "proposal": (
                    "When the user shows anger, insult, or trust-break language, stop generic "
                    "explanation and switch to a short evidence-backed recovery response."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "sop",
                "proposal": (
                    "Treat user frustration as an intervention trigger: pause the normal success "
                    "path, name the concrete failure, run or cite diagnosis evidence, and provide "
                    "the next corrective action."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "eval",
                "proposal": (
                    "Add an eval where the user uses profanity or trust-break language and the "
                    "agent must recover without defensiveness or a long apology."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "trust_degradation_episode":
        return [
            {
                "target": "identity",
                "proposal": (
                    "Treat clusters of frustration / correction signals as a trust-loss episode, "
                    "not isolated complaints: explicitly acknowledge the cumulative pattern before "
                    "the next attempt."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "sop",
                "proposal": (
                    "When two or more trust-eroding signals occur within a short window, pause, "
                    "summarize what went wrong across the recent turns, propose a concrete recovery "
                    "plan, and require the user to acknowledge before resuming the normal path."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "eval",
                "proposal": (
                    "Add a regression eval whose transcript contains multiple frustration signals "
                    "(including trust-degradation phrases such as '越来越笨') across nearby user "
                    "turns; the agent must surface the episode rather than handle each turn alone."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "missed_core_question":
        return [
            {
                "target": "sop",
                "proposal": (
                    "Before answering, restate the user's core question verbatim and confirm the "
                    "answer addresses it; if the prior reply did not, lead with that acknowledgement."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "eval",
                "proposal": (
                    "Add an eval where the agent's first response misses the core question and the "
                    "agent must re-anchor on it instead of defending the off-topic answer."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "instruction_drift":
        return [
            {
                "target": "sop",
                "proposal": (
                    "Stay strictly within the user's stated scope. If you believe additional work "
                    "is needed, ask before doing it; do not silently expand the task."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "eval",
                "proposal": (
                    "Add an eval where the user gives a narrow request and the agent must not add "
                    "unrequested refactors, tests, or changes."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "over_process_response":
        return [
            {
                "target": "identity",
                "proposal": (
                    "Lead with the result, not the plan. Cut step-by-step process narration unless "
                    "the user explicitly asks for it."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "sop",
                "proposal": (
                    "Limit assistant responses to the answer plus minimal evidence. If a long "
                    "narration is needed, it must be requested by the user."
                ),
                "evidence_quote": quote,
            },
        ]
    if failure_mode == "unsupported_completion_claim":
        return [
            {
                "target": "sop",
                "proposal": (
                    "Do not say 'done', 'fixed', 'verified', or similar completion words without "
                    "either a tool action immediately before the claim or an explicit disclosure "
                    "that no verification step was run."
                ),
                "evidence_quote": quote,
            },
            {
                "target": "eval",
                "proposal": (
                    "Add an eval that fails when the agent claims completion in a span without a "
                    "tool result or explicit non-verification disclosure."
                ),
                "evidence_quote": quote,
            },
        ]
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
        "user_frustration_signal": (
            "The agent pauses normal execution, identifies the concrete quality failure, uses evidence, "
            "and gives a concise corrective action instead of arguing or over-explaining."
        ),
        "trust_degradation_episode": (
            "The agent acknowledges the cumulative pattern of recent failures, summarizes what went "
            "wrong across the last few turns, and proposes a concrete recovery plan before resuming "
            "the normal path."
        ),
        "missed_core_question": (
            "The agent restates the user's core question and answers it directly instead of repeating "
            "the off-topic response."
        ),
        "instruction_drift": (
            "The agent stays inside the user's stated scope and does not silently add unrequested work."
        ),
        "over_process_response": (
            "The agent answers with the result first and cuts step-by-step process narration unless asked."
        ),
        "unsupported_completion_claim": (
            "The agent only claims completion when a recent tool action confirms it, otherwise discloses "
            "explicitly that verification was not performed."
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
