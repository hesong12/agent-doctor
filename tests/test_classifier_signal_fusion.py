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


def test_repeat_theme_detects_repeated_chinese_phrase_across_turns() -> None:
    messages = [
        "这个 memory 状态不对。",
        "memory 同步还是不对。",
        "先把 memory 状态查清楚。",
    ]
    assert score_repeat_themes(messages) == 1


def test_repeat_theme_ignores_single_long_chinese_question() -> None:
    messages = [
        "我们聊一下ai harness的事情，scripts/submit-work.sh 这个东西是我们之前搞的repo scoped的规范，这个是放在了你的skill里面了吗？还是在哪里？这种类似的规范本身是有体现在我们目前的ai harness的设计和实现中吗？我们有什么办法可以做到一个unified submit-work的harness，这样可以更加的规范和确定性一些？"
    ]
    assert score_repeat_themes(messages) == 0


def test_repeat_theme_ignores_chinese_stopword_density() -> None:
    messages = [
        "这个事情是一个需要整体看的事情。",
        "这个规范本身是在哪里体现的？",
        "我们是不是可以用一个统一入口？",
    ]
    assert score_repeat_themes(messages) == 0


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

def test_repeat_theme_counts_meaningful_single_cjk_tokens() -> None:
    messages = ["你很笨", "还是笨", "笨"]

    assert score_repeat_themes(messages) == 1


def test_repeat_theme_does_not_fire_on_repeated_technical_cjk_nouns() -> None:
    messages = [
        "代码运行路径需要看一下",
        "代码运行日志在哪里",
        "代码运行环境是 Python 3.12",
    ]

    assert score_repeat_themes(messages) == 0
