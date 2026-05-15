"""Lock in the set of CLI subcommands that the desktop pet UI relies on.

The Swift pet shells out to ``python -m agent_doctor.cli <sub>`` for every
user action: ``settings set-gemini-key``, ``pet-generate-sprite``,
``pet-set-sprite``, ``pet-usage``, ``pet-action``. If any of these stops
being registered on the argparse parser, the pet UI surfaces a generic
"agent-doctor: error: invalid choice" banner with no actionable detail —
the exact failure mode that masked three downstream bugs in a single
session.

These tests are intentionally cheap (no subprocess, no install layer):
they inspect the parser the CLI builds at import time. A subcommand
disappearing from the source is caught before CI even installs Node /
Pillow / google-genai.
"""

from __future__ import annotations

import argparse

from agent_doctor import cli


def _subparser_choices(parser: argparse.ArgumentParser) -> set[str]:
    """Return every subcommand name across all ``_SubParsersAction`` groups
    on ``parser``.

    A parser can in principle have more than one ``_SubParsersAction`` (e.g.
    after a refactor that splits commands into groups). Returning only the
    first group's choices would silently under-report the parser's surface;
    we accumulate across all groups so this guard does not regress quietly
    if the CLI structure evolves.
    """

    choices: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            choices.update(action.choices)
    return choices


def _nested_subparser(
    parser: argparse.ArgumentParser, parent: str
) -> argparse.ArgumentParser:
    """Return the child parser registered as ``parent`` under any
    ``_SubParsersAction`` on ``parser``.

    Like :func:`_subparser_choices`, we look across every subparser group
    rather than the first one. We also check membership before indexing so
    a missing ``parent`` yields the friendlier ``AssertionError`` below
    instead of a raw ``KeyError`` from the first group that did not happen
    to contain the command.
    """

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            if parent in action.choices:
                return action.choices[parent]
    raise AssertionError(
        f"no subparser named {parent!r} found on parser "
        f"(checked {sum(1 for a in parser._actions if isinstance(a, argparse._SubParsersAction))} "
        f"subparser group(s))"
    )


def test_top_level_subcommands_registered() -> None:
    """Every subcommand the Swift pet UI invokes must be on the parser.

    Removing or renaming one of these silently breaks a pet menu action
    because the Swift caller hardcodes the subcommand name.
    """

    parser = cli.build_parser()
    choices = _subparser_choices(parser)

    required = {
        # Pet UI menu → "Set Gemini key" / "Clear Gemini key" / settings show.
        "settings",
        # Pet UI menu → "Generate sprite from prompt...".
        "pet-generate-sprite",
        # Pet UI menu → "Change sprite..." (file picker).
        "pet-set-sprite",
        # Pet UI popover → single-click on the pet (Claude + Codex usage).
        "pet-usage",
        # Pet UI dialog buttons (tell agent, dismiss, diagnose...).
        "pet-action",
        # Pet UI hot-reload loop reads the status file written by `pet`.
        "pet",
        # Swift pet itself is launched via `pet-display`.
        "pet-display",
    }

    missing = required - choices
    assert not missing, (
        f"top-level subcommands missing from parser: {sorted(missing)}. "
        f"Swift pet UI would surface 'invalid choice' for each. "
        f"Currently registered: {sorted(choices)}"
    )


def test_settings_subcommands_registered() -> None:
    """The settings group must include set/clear/show.

    Swift's ``runSetGeminiKeyProcess`` calls
    ``settings set-gemini-key --from-env <var>``; ``runClearGeminiKey``
    calls ``settings clear-gemini-key --yes``. Either disappearing
    silently breaks "Set Gemini key" in the pet menu.
    """

    parser = cli.build_parser()
    settings_parser = _nested_subparser(parser, "settings")
    nested = _subparser_choices(settings_parser)

    required = {"set-gemini-key", "clear-gemini-key", "show"}
    missing = required - nested
    assert not missing, (
        f"settings/* subcommands missing: {sorted(missing)}. "
        f"Currently registered: {sorted(nested)}"
    )


def test_set_gemini_key_accepts_from_env_flag() -> None:
    """``settings set-gemini-key --from-env <var>`` is the exact form
    Swift's runSetGeminiKeyProcess passes. Parsing it must succeed —
    the key is intentionally never on argv (would leak via ``ps``),
    so this flag is load-bearing for the pet UI.
    """

    parser = cli.build_parser()
    args = parser.parse_args(
        ["settings", "set-gemini-key", "--from-env", "FAKE_ENV_VAR"]
    )
    assert getattr(args, "from_env", None) == "FAKE_ENV_VAR"


def test_pet_generate_sprite_accepts_prompt_and_out() -> None:
    """The exact argv Swift sends after the user types a prompt.

    --out lets the test suite (and Swift) redirect away from the real
    user sprite path; --prompt is the user's text. If either name
    changes, the pet UI will silently fail.
    """

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "pet-generate-sprite",
            "--prompt",
            "a smiling pixel cat",
            "--out",
            "/tmp/test.png",
        ]
    )
    assert args.prompt == "a smiling pixel cat"
    assert str(args.out) == "/tmp/test.png"


def test_pet_usage_accepts_json_flag() -> None:
    """Swift's runUsageCollect runs ``pet-usage --json`` and parses
    stdout as a JSON dict. The flag must remain on the parser; if it
    becomes positional or renamed, the popover gets an empty body.
    """

    parser = cli.build_parser()
    args = parser.parse_args(["pet-usage", "--json"])
    assert getattr(args, "as_json", False) is True


# --------------------------------------------------------------------------- #
# Regression tests for PR #25 review feedback (Gemini)                        #
# --------------------------------------------------------------------------- #


def test_subparser_choices_empty_when_no_subparsers() -> None:
    """No ``_SubParsersAction`` on the parser -> empty set, not crash."""

    p = argparse.ArgumentParser()
    p.add_argument("--flag")
    assert _subparser_choices(p) == set()


def _build_parser_with_two_subparser_groups() -> argparse.ArgumentParser:
    """argparse rejects ``add_subparsers`` being called twice on the same
    parser ("cannot have multiple subparser arguments"). The defensive
    helpers still iterate across every ``_SubParsersAction`` for clarity
    and forward-compat, so we exercise that path by building one group
    normally and grafting a second ``_SubParsersAction`` onto the parser's
    ``_actions`` list directly. This mirrors what a future argparse fork or
    a manual hand-roll of a parser tree could look like and pins the
    aggregation contract.
    """

    parser = argparse.ArgumentParser()
    sub_a = parser.add_subparsers(dest="group_a")
    sub_a.add_parser("alpha")
    sub_a.add_parser("beta")

    side = argparse.ArgumentParser()
    sub_b = side.add_subparsers(dest="group_b")
    sub_b.add_parser("gamma")
    # Pull the side parser's _SubParsersAction off and graft it onto the
    # primary parser's _actions list so _subparser_choices sees both.
    for action in side._actions:
        if isinstance(action, argparse._SubParsersAction):
            parser._actions.append(action)
            break
    return parser


def test_subparser_choices_aggregates_across_multiple_groups() -> None:
    """Gemini #25 medium: _subparser_choices must aggregate every
    _SubParsersAction it finds, not return on the first one. The bug cannot
    arise from the public argparse API today, but the helper's behavior is
    pinned so it stays correct under refactors / argparse forks."""

    parser = _build_parser_with_two_subparser_groups()
    assert _subparser_choices(parser) == {"alpha", "beta", "gamma"}


def test_nested_subparser_finds_command_in_later_group() -> None:
    """Companion: _nested_subparser must also look across every
    _SubParsersAction, not raise KeyError on the first that lacks the
    command."""

    parser = _build_parser_with_two_subparser_groups()
    found = _nested_subparser(parser, "gamma")
    assert isinstance(found, argparse.ArgumentParser)


def test_nested_subparser_missing_raises_friendly_assertion() -> None:
    """Gemini #25 medium: a missing subcommand must surface as the helper's
    AssertionError with a useful diagnostic, not a raw KeyError from
    argparse internals."""

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="group")
    sub.add_parser("alpha")

    import pytest

    with pytest.raises(AssertionError, match=r"no subparser named 'missing'"):
        _nested_subparser(p, "missing")


EXPECTED_DICTATE_MODELS_SUBCOMMANDS = {
    "list",
    "current",
    "download",
    "set",
    "remove",
    "doctor",
}


def test_dictate_models_subcommands_registered() -> None:
    parser = cli.build_parser()
    dictate_sub = _nested_subparser(parser, "dictate")
    models_sub = _nested_subparser(dictate_sub, "models")
    assert _subparser_choices(models_sub) >= EXPECTED_DICTATE_MODELS_SUBCOMMANDS


EXPECTED_DICTATE_LLM_SUBCOMMANDS = {"probe", "set", "current", "test"}


def test_dictate_llm_subcommands_registered() -> None:
    parser = cli.build_parser()
    dictate_sub = _nested_subparser(parser, "dictate")
    llm_sub = _nested_subparser(dictate_sub, "llm")
    assert _subparser_choices(llm_sub) >= EXPECTED_DICTATE_LLM_SUBCOMMANDS


EXPECTED_DICTATE_HOTKEY_SUBCOMMANDS = {"install", "set", "show", "test", "uninstall"}


def test_dictate_hotkey_subcommands_registered() -> None:
    parser = cli.build_parser()
    dictate_sub = _nested_subparser(parser, "dictate")
    hk_sub = _nested_subparser(dictate_sub, "hotkey")
    assert _subparser_choices(hk_sub) >= EXPECTED_DICTATE_HOTKEY_SUBCOMMANDS
