"""Tests for signal_fusion."""
from agent_doctor.classifier.signal_fusion import (
    SignalScores,
    fuse_signals,
    score_repeat_themes,
    score_trajectory,
    score_typing_shape,
)


def test_typing_shape_punctuation_density() -> None:
    assert score_typing_shape("this is fine") == 0
    assert score_typing_shape("WHAT??!?") == 2  # punct + caps
    assert score_typing_shape("again???") == 1
    assert score_typing_shape("WHY DOES THIS NEVER WORK") == 1  # caps but no punct


def test_typing_shape_chinese_punctuation() -> None:
    assert score_typing_shape("为什么？？？") == 1


def test_trajectory_with_few_messages_is_zero() -> None:
    assert score_trajectory(["a"]) == 0
    assert score_trajectory(["a", "b"]) == 0


def test_trajectory_detects_length_collapse() -> None:
    """User goes from paragraphs to one-word retorts → escalation."""
    messages = [
        "Could you please look at this and tell me what you think the right approach is?",
        "Also consider the edge cases and the existing patterns we've established before.",
        "wrong",
        "no",
        "again",
    ]
    assert score_trajectory(messages) >= 1


def test_repeat_theme_detects_repeated_word() -> None:
    messages = [
        "memory is wrong",
        "again the memory issue",
        "can you fix the memory please",
    ]
    assert score_repeat_themes(messages) == 1


def test_repeat_theme_zero_when_under_threshold() -> None:
    messages = ["alpha beta", "gamma delta"]
    assert score_repeat_themes(messages) == 0


def test_fuse_signals_combines_all() -> None:
    messages = [
        "memory is broken!",
        "still broken!!",
        "MEMORY!!!",
    ]
    scores = fuse_signals(text=messages[-1], recent_user_messages=messages)
    assert isinstance(scores, SignalScores)
    assert scores.total >= 1  # something fires
    assert scores.typing_shape >= 1


def test_fuse_signals_works_without_history() -> None:
    scores = fuse_signals(text="WHY DOES THIS NEVER WORK??")
    assert scores.trajectory == 0
    assert scores.repeat_theme == 0
    assert scores.typing_shape >= 1
