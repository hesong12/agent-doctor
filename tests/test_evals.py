"""Tests for the eval subsystem: cards, generator, bench, replay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_doctor.evals.bench import run_benchmark
from agent_doctor.evals.cards import CardError, ScenarioCard, load_card, load_cards
from agent_doctor.evals.generator import generate_corpus
from agent_doctor.evals.metrics import evaluate
from agent_doctor.evals.replay import run_replay
from agent_doctor.schema import Finding, Evidence

FIXTURES = Path(__file__).parent / "fixtures"
CARDS_DIR = FIXTURES / "cards"


# ---- cards ----------------------------------------------------------------


def test_load_cards_reads_all_yaml_files() -> None:
    cards = load_cards(CARDS_DIR)
    assert len(cards) == 6
    ids = {card.id for card in cards}
    assert "SCEN-001" in ids
    assert all(isinstance(card, ScenarioCard) for card in cards)


def test_load_card_rejects_unknown_failure_mode(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: BAD-1\n"
        "task: x\n"
        "agent_persona: y\n"
        "user_persona: z\n"
        "length_turns: 4\n"
        "output_format: hermes\n"
        "seeded_failures:\n"
        "  - mode: not_a_real_mode\n"
        "    turn: 2\n"
        "    severity: medium\n",
        encoding="utf-8",
    )
    with pytest.raises(CardError):
        load_card(bad)


def test_load_card_rejects_invalid_output_format(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: BAD-2\n"
        "task: x\n"
        "agent_persona: y\n"
        "user_persona: z\n"
        "length_turns: 4\n"
        "output_format: nonsense\n"
        "seeded_failures: []\n",
        encoding="utf-8",
    )
    with pytest.raises(CardError):
        load_card(bad)


def test_load_card_accepts_json(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "id": "JSON-1",
                "task": "do a thing",
                "agent_persona": "agent",
                "user_persona": "user",
                "length_turns": 4,
                "output_format": "generic",
                "seeded_failures": [],
                "distractors": [],
            }
        ),
        encoding="utf-8",
    )
    card = load_card(good)
    assert card.id == "JSON-1"


# ---- generator ------------------------------------------------------------


def test_generate_corpus_produces_transcripts_and_labels(tmp_path: Path) -> None:
    summary = generate_corpus(CARDS_DIR, tmp_path / "corpus")
    assert summary["card_count"] == 6
    assert summary["transcripts"] == 6
    assert summary["llm_fallbacks"] == 0
    assert (tmp_path / "corpus" / "INDEX.json").exists()
    transcripts = list((tmp_path / "corpus" / "transcripts").iterdir())
    labels = list((tmp_path / "corpus" / "labels").iterdir())
    assert len(transcripts) == 6
    assert len(labels) == 6


def test_generated_transcripts_are_deterministic_with_same_seed(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    generate_corpus(CARDS_DIR, a, seed=42)
    generate_corpus(CARDS_DIR, b, seed=42)
    for name in ("SCEN-001.jsonl", "SCEN-002.jsonl", "SCEN-006.jsonl"):
        assert (a / "transcripts" / name).read_bytes() == (b / "transcripts" / name).read_bytes()


def test_generated_corpus_has_no_findings_for_distractor_only_card(tmp_path: Path) -> None:
    """SCEN-006 has no seeded failures; the detectors must not fire on it."""

    from agent_doctor.detectors import detect_findings
    from agent_doctor.ingest import ingest_path

    generate_corpus(CARDS_DIR, tmp_path / "corpus")
    transcript = tmp_path / "corpus" / "transcripts" / "SCEN-006.jsonl"
    findings = detect_findings(ingest_path(transcript))
    assert findings == []


# ---- metrics --------------------------------------------------------------


def test_metrics_handles_missed_label_as_false_negative() -> None:
    findings_by_session: dict[str, list[Finding]] = {"s1": []}
    labels_by_session = {"s1": [{"mode": "memory_failure", "severity": "medium"}]}
    metrics = evaluate(findings_by_session, labels_by_session)
    assert metrics["per_mode"]["memory_failure"]["fn"] == 1
    assert metrics["total"]["recall"] == 0.0


def test_metrics_handles_unlabeled_finding_as_false_positive() -> None:
    finding = Finding(
        id="x",
        severity="medium",
        failure_mode="memory_failure",
        title="t",
        evidence=[Evidence("f", 1, "user", "q")],
        diagnosis="d",
        recommendations=[],
        eval_case={},
        confidence=0.8,
        session_id="s1",
        count=1,
    )
    metrics = evaluate({"s1": [finding]}, {"s1": []})
    assert metrics["per_mode"]["memory_failure"]["fp"] == 1
    assert metrics["total"]["precision"] == 0.0


# ---- bench ----------------------------------------------------------------


def test_run_benchmark_reaches_perfect_score_on_seeded_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    bench_out = tmp_path / "bench"
    generate_corpus(CARDS_DIR, corpus)

    result = run_benchmark(corpus, bench_out)
    assert result.transcript_count == 6
    assert result.total["precision"] == 1.0
    assert result.total["recall"] == 1.0
    assert result.total["f1"] == 1.0
    assert (bench_out / "bench.json").exists()
    assert (bench_out / "bench.md").exists()


def test_cli_eval_bench_with_failing_gates_exits_nonzero(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    bench_out = tmp_path / "bench"
    generate_corpus(CARDS_DIR, corpus)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "eval",
            "bench",
            "--corpus",
            str(corpus),
            "--out",
            str(bench_out),
            "--gate-precision",
            "1.5",  # impossible gate
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "precision gate failed" in result.stderr


def test_cli_eval_bench_with_passing_gates_exits_zero(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    bench_out = tmp_path / "bench"
    generate_corpus(CARDS_DIR, corpus)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "eval",
            "bench",
            "--corpus",
            str(corpus),
            "--out",
            str(bench_out),
            "--gate-precision",
            "0.95",
            "--gate-recall",
            "0.9",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


# ---- replay ---------------------------------------------------------------


def test_replay_no_op_without_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    transcript = tmp_path / "tx.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "session": "s1",
                "role": "user",
                "message": "Did you actually test it?",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "sop.md").write_text("# sop", encoding="utf-8")

    out = tmp_path / "out"
    summary = run_replay(transcript, patches, out)

    assert summary["enabled"] is False
    assert "ANTHROPIC_API_KEY" in summary["reason"]
    assert summary["baseline"]["findings"] >= 1
    assert (out / "replay-summary.json").exists()
