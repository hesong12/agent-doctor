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
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _nested_subparser(
    parser: argparse.ArgumentParser, parent: str
) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[parent]
    raise AssertionError(f"no subparsers action on parser; expected one with {parent!r}")


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
