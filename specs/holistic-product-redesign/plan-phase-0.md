# Phase 0 Implementation Plan — Foundation Fix

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the working-tree delivery fix so OpenClaw interventions stop silently failing under launchd's minimal PATH, capture the actual subprocess error message in `delivery-errors.jsonl` (today only `"returned non-zero exit status 1"` is recorded — the real reason is discarded), and add an end-to-end regression test that proves the fix works from a launchd-equivalent environment.

**Architecture:** Three atomic tasks: (1) replace `str(CalledProcessError)` with a structured capture of return code + stderr + stdout in `autopilot.run_notify_command`, (2) add an integration test that combines `resolve_openclaw_binary` with a real subprocess call from a tempdir CWD with minimal PATH, (3) commit the existing working tree changes plus the new improvements as one Phase 0 commit.

**Tech Stack:** Python 3.11+, pytest, the existing `agent_doctor.autopilot` and `agent_doctor.delivery` modules.

---

## Task 1: Capture full subprocess error in `run_notify_command`

**Why:** Today `delivery-errors.jsonl` records `"Command '...' returned non-zero exit status 1."` because `run_notify_command` calls `str(exc)` on a `CalledProcessError`, which throws away the actual stderr (`agent-doctor: error: openclaw binary not found: openclaw`). After this task, the JSONL line includes the real reason so future failures are debuggable.

**Files:**
- Modify: `agent_doctor/autopilot.py` (function `run_notify_command` around lines 364-387)
- Test: `tests/test_autopilot.py` (add new test alongside existing delivery-failure tests around line 235)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_autopilot.py`:

```python
def test_run_notify_command_captures_subprocess_stderr(tmp_path: Path) -> None:
    """Failed notify subprocess: stderr is captured in the error string.

    Today the error string is just CalledProcessError's str(), which is
    'Command ... returned non-zero exit status N.' That hides why the
    subprocess actually failed. After this fix the error string includes
    rc + stderr + stdout so delivery-errors.jsonl is debuggable.
    """
    transcript = tmp_path / "session.jsonl"
    state = tmp_path / "doctor" / "state.sqlite3"
    _write_jsonl(
        transcript,
        [
            {
                "session_id": "s-stderr",
                "role": "user",
                "content": "你怎么这么笨，又搞错了",
            }
        ],
    )

    notify = (
        f"{sys.executable} -c "
        "\"import sys; sys.stderr.write('boom: openclaw not found\\n'); "
        "sys.stdout.write('partial stdout\\n'); sys.exit(1)\""
    )

    result = run_autopilot_once(
        platform="generic",
        path=transcript,
        out_dir=tmp_path / "doctor",
        state_path=state,
        notify_command=notify,
    )

    assert len(result.events) == 1
    assert result.delivery_errors, "delivery should have failed"
    err = result.delivery_errors[0]
    assert "rc=1" in err
    assert "boom: openclaw not found" in err
    assert "partial stdout" in err
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/songhe/Projects/agent-doctor
python3 -m pytest tests/test_autopilot.py::test_run_notify_command_captures_subprocess_stderr -v
```

Expected: FAIL with `AssertionError: assert 'rc=1' in 'Command ...returned non-zero exit status 1.'`

- [ ] **Step 3: Update `run_notify_command` to capture stderr and stdout**

Edit `agent_doctor/autopilot.py`. Replace the existing `run_notify_command` body (currently around lines 383-387) with:

```python
    try:
        completed = subprocess.run(
            args,
            check=False,  # we want to inspect the result, not raise
            text=True,
            capture_output=True,
            timeout=15,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return f"timeout after {exc.timeout}s: {' '.join(args)}"
    except OSError as exc:
        return f"could not start notify command {args!r}: {exc}"
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip() if completed.stderr else ""
        stdout_tail = completed.stdout.strip() if completed.stdout else ""
        parts = [f"rc={completed.returncode}"]
        if stderr_tail:
            parts.append(f"stderr={stderr_tail!r}")
        if stdout_tail:
            parts.append(f"stdout={stdout_tail!r}")
        return " ".join(parts)
    return None
```

The full function after the change should look like:

```python
def run_notify_command(command: str, event: AutopilotEvent) -> str | None:
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return f"invalid notify command: {exc}"
    if not args:
        return "empty notify command"
    env = os.environ.copy()
    env.update(
        {
            "AGENT_DOCTOR_EVENT_ID": event.id,
            "AGENT_DOCTOR_TRIGGER": event.trigger,
            "AGENT_DOCTOR_SEVERITY": event.severity,
            "AGENT_DOCTOR_ACTION": event.action,
            "AGENT_DOCTOR_SESSION_ID": event.session_id,
            "AGENT_DOCTOR_CARD": event.card_path or "",
            "AGENT_DOCTOR_SUMMARY": event.summary,
        }
    )
    try:
        completed = subprocess.run(
            args,
            check=False,
            text=True,
            capture_output=True,
            timeout=15,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return f"timeout after {exc.timeout}s: {' '.join(args)}"
    except OSError as exc:
        return f"could not start notify command {args!r}: {exc}"
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip() if completed.stderr else ""
        stdout_tail = completed.stdout.strip() if completed.stdout else ""
        parts = [f"rc={completed.returncode}"]
        if stderr_tail:
            parts.append(f"stderr={stderr_tail!r}")
        if stdout_tail:
            parts.append(f"stdout={stdout_tail!r}")
        return " ".join(parts)
    return None
```

- [ ] **Step 4: Run the new test to verify it passes**

```bash
cd /Users/songhe/Projects/agent-doctor
python3 -m pytest tests/test_autopilot.py::test_run_notify_command_captures_subprocess_stderr -v
```

Expected: PASS.

- [ ] **Step 5: Run the existing delivery-failure tests to make sure they still pass**

The existing tests (`test_autopilot_retries_intervention_after_delivery_failure` and `test_autopilot_records_successful_delivery_for_cooldown`) assert specific error-string substrings. The new format keeps `rc=N` instead of `returned non-zero exit status N`, so the existing assertion `"returned non-zero exit status 7" in first.delivery_errors[0]` will break. Update it.

In `tests/test_autopilot.py`, find:

```python
    assert "returned non-zero exit status 7" in first.delivery_errors[0]
```

Replace with:

```python
    assert "rc=7" in first.delivery_errors[0]
```

- [ ] **Step 6: Run the full test suite**

```bash
cd /Users/songhe/Projects/agent-doctor
python3 -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 7: Stage the changes (do NOT commit yet — Task 3 commits the whole Phase 0 bundle)**

```bash
git add agent_doctor/autopilot.py tests/test_autopilot.py
```

---

## Task 2: End-to-end launchd-PATH regression test

**Why:** Existing unit tests cover `resolve_openclaw_binary` with monkeypatched HOST_BIN_DIRS, and there are mocked-subprocess tests in `test_delivery.py`. What's missing is a single integration test that actually runs a subprocess from a tempdir CWD with launchd's minimal PATH and asserts the full notify path works end-to-end. If this test had existed before, the original bug wouldn't have shipped.

**Files:**
- Test: `tests/test_delivery.py` (add new test at end of file)

- [ ] **Step 1: Write the failing test**

Add to the end of `tests/test_delivery.py`:

```python
def test_notify_openclaw_system_event_works_under_launchd_minimal_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Integration test reproducing the original launchd failure mode.

    With launchd's default PATH (/usr/bin:/bin:/usr/sbin:/sbin), bare
    'openclaw' is not findable. Before resolve_openclaw_binary, this
    failed with FileNotFoundError -> RuntimeError -> exit 1. After the
    fix, HOST_BIN_DIRS resolves the binary even with a stripped PATH.
    """
    fake_openclaw = tmp_path / "openclaw"
    fake_openclaw.write_text(
        "#!/bin/sh\necho ok\nexit 0\n",
        encoding="utf-8",
    )
    fake_openclaw.chmod(0o755)
    monkeypatch.setattr(
        "agent_doctor.delivery.HOST_BIN_DIRS",
        (str(tmp_path),),
    )

    card = tmp_path / "card.md"
    card.write_text("card body", encoding="utf-8")

    env = _event_env(card) | {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }

    result = notify_openclaw_system_event(env=env)

    assert result.delivered is True, (
        f"expected delivery, got skipped={result.skipped} stderr={result.stderr!r}"
    )
    assert result.command[0] == str(fake_openclaw), (
        "openclaw should be resolved to absolute path even with minimal PATH"
    )
    assert "ok" in result.stdout
```

- [ ] **Step 2: Run the test to verify it passes** (the working-tree fix is already in place, so this should pass)

```bash
cd /Users/songhe/Projects/agent-doctor
python3 -m pytest tests/test_delivery.py::test_notify_openclaw_system_event_works_under_launchd_minimal_path -v
```

Expected: PASS.

- [ ] **Step 3: Confirm the test would have caught the bug** by temporarily breaking `resolve_openclaw_binary`

This is a one-time validation that the test really protects against regression. Do not commit the temporary break.

In `agent_doctor/delivery.py`, temporarily edit `resolve_openclaw_binary` so it just returns the input bare:

```python
def resolve_openclaw_binary(openclaw_bin: str = "openclaw", *, env: Mapping[str, str] | None = None) -> str:
    return openclaw_bin  # TEMPORARY — for regression-test validation only
```

- [ ] **Step 4: Re-run the new test and confirm it now FAILS**

```bash
python3 -m pytest tests/test_delivery.py::test_notify_openclaw_system_event_works_under_launchd_minimal_path -v
```

Expected: FAIL with a `FileNotFoundError`-derived `RuntimeError("openclaw binary not found: openclaw")`.

- [ ] **Step 5: Restore `resolve_openclaw_binary` to its real implementation**

```bash
git checkout agent_doctor/delivery.py
```

- [ ] **Step 6: Re-run the test to confirm it passes again**

```bash
python3 -m pytest tests/test_delivery.py::test_notify_openclaw_system_event_works_under_launchd_minimal_path -v
```

Expected: PASS.

- [ ] **Step 7: Run the full test suite to confirm no regressions**

```bash
python3 -m pytest -q
```

Expected: all tests pass (140 in total — 139 existing + 1 new from Task 1 + 1 new from this task = 141; verify the count).

- [ ] **Step 8: Stage the test addition**

```bash
git add tests/test_delivery.py
```

---

## Task 3: Commit the Phase 0 bundle

**Why:** The working tree contains the deliberate fix (`resolve_openclaw_binary`, plist `Environment=PATH=...`, don't-record-on-failure, doc updates) plus the two improvements from Tasks 1-2. Land it all as one atomic Phase 0 commit so the bug fix and its regression coverage stay together.

**Files:**
- All currently modified files plus the new tests:
  - `README.md`
  - `agent_doctor/autopilot.py`
  - `agent_doctor/delivery.py`
  - `agent_doctor/service.py`
  - `docs/architecture.md`
  - `tests/test_autopilot.py`
  - `tests/test_delivery.py`
  - `tests/test_service.py`

- [ ] **Step 1: Confirm the staged set is exactly what Phase 0 expects**

```bash
git status --short
```

Expected output (order may vary):

```
M  README.md
M  agent_doctor/autopilot.py
M  agent_doctor/delivery.py
M  agent_doctor/service.py
M  docs/architecture.md
M  tests/test_autopilot.py
M  tests/test_delivery.py
M  tests/test_service.py
```

If any of these eight files is missing from staging, run:

```bash
git add README.md agent_doctor/autopilot.py agent_doctor/delivery.py \
        agent_doctor/service.py docs/architecture.md \
        tests/test_autopilot.py tests/test_delivery.py tests/test_service.py
```

- [ ] **Step 2: Run the full test suite one more time before commit**

```bash
python3 -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Create the Phase 0 commit**

```bash
git commit -m "$(cat <<'EOF'
fix: resolve openclaw under launchd minimal PATH + capture real notify errors

The autopilot's launchd service inherits PATH=/usr/bin:/bin:/usr/sbin:/sbin
which excludes /opt/homebrew/bin where Homebrew installs openclaw on Apple
Silicon. The installed delivery code called subprocess.run(["openclaw", ...])
with bare command, hit FileNotFoundError, surfaced as exit 1, and the
autopilot's run_notify_command then logged only str(CalledProcessError) —
"returned non-zero exit status 1" — discarding the real reason.

Three changes land together so Phase 0 is fully self-contained:

1. agent_doctor/delivery.py: resolve_openclaw_binary searches HOST_BIN_DIRS
   when PATH lookup fails. _openclaw_subprocess_env prepends host bin dirs
   to PATH so the resolved binary's downstream subprocess calls also work.
2. agent_doctor/service.py: launchd plist and systemd unit now set
   PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
   so the autopilot service inherits a host-correct PATH from boot.
3. agent_doctor/autopilot.py: run_notify_command now captures rc + stderr +
   stdout structurally instead of stringifying CalledProcessError, so
   delivery-errors.jsonl records the actual failure reason. Failed
   deliveries no longer mark the event as handled in SQLite, allowing the
   next watch pass to retry instead of hiding the recovery moment behind
   cooldown.

Tests:
- test_resolve_openclaw_binary_uses_host_paths_when_launchd_path_is_minimal
- test_notify_openclaw_system_event_works_under_launchd_minimal_path
  (full integration: real subprocess, stripped PATH, host-bin resolution)
- test_run_notify_command_captures_subprocess_stderr
- test_autopilot_retries_intervention_after_delivery_failure
- test_autopilot_records_successful_delivery_for_cooldown

Refs: specs/holistic-product-redesign/{requirements,design,tasks}.md (Phase 0)
EOF
)"
```

- [ ] **Step 4: Verify the commit landed cleanly**

```bash
git log -1 --stat
```

Expected: shows the commit with all eight files. Working tree should be clean (no uncommitted changes other than `.claude/`).

```bash
git status --short
```

Expected: only `?? .claude/` (untracked, harmless).

---

## Task 4: Reinstall pipx + restart launchd service so the fix is actually running

**Why:** Committing the fix to git doesn't make it run. The launchd service is using `/Users/songhe/.local/pipx/venvs/agent-doctor/lib/python3.14/site-packages/agent_doctor/delivery.py` which is the May 4 22:40 snapshot from the original `pipx install`. Without re-installing, the running service keeps failing exactly as before.

**Files:**
- Re-installs pipx package
- Re-generates launchd plist via `agent-doctor setup autopilot`
- Verifies the service picks up the new code

- [ ] **Step 1: Reinstall the pipx package from the local source tree**

```bash
pipx install --force /Users/songhe/Projects/agent-doctor
```

Expected: `installed package agent-doctor 0.3.0` (or newer). pipx should report it copied the new files.

- [ ] **Step 2: Verify pipx site-packages now has resolve_openclaw_binary**

```bash
grep -c "resolve_openclaw_binary" \
  /Users/songhe/.local/pipx/venvs/agent-doctor/lib/python*/site-packages/agent_doctor/delivery.py
```

Expected: a number `> 0` (not zero). If zero, pipx didn't update — repeat Step 1 with `pipx uninstall agent-doctor && pipx install /Users/songhe/Projects/agent-doctor`.

- [ ] **Step 3: Regenerate the launchd plist via `setup autopilot`**

```bash
agent-doctor setup autopilot
```

Expected: prints "Wrote launchd service: /Users/songhe/Library/LaunchAgents/com.agentdoctor.openclaw.plist" and either "Service started" or restart commands listed. The new plist now contains `Environment=PATH=...`.

- [ ] **Step 4: Verify the new plist contains PATH**

```bash
grep -A1 "<key>PATH</key>" ~/Library/LaunchAgents/com.agentdoctor.openclaw.plist
```

Expected:

```
		<key>PATH</key>
		<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
```

- [ ] **Step 5: Confirm the service is running with the new code**

```bash
launchctl list | grep agentdoctor
```

Expected: line showing `com.agentdoctor.openclaw` with a recent PID.

- [ ] **Step 6: Smoke-test delivery from a launchd-equivalent context**

```bash
cd /tmp && env -i \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  HOME=/Users/songhe \
  AGENT_DOCTOR_HOST_HOME=/Users/songhe \
  AGENT_DOCTOR_EVENT_ID=phase0_smoke \
  AGENT_DOCTOR_TRIGGER=user_frustration_signal \
  AGENT_DOCTOR_SEVERITY=high \
  AGENT_DOCTOR_ACTION=intervene \
  AGENT_DOCTOR_SESSION_ID=phase0_smoke \
  AGENT_DOCTOR_CARD="" \
  AGENT_DOCTOR_SUMMARY=phase0_smoke_test \
  /Users/songhe/.local/pipx/venvs/agent-doctor/bin/python \
  -m agent_doctor.cli notify openclaw-system-event --dry-run | head -5
```

Expected: JSON output with `"delivered": false` (because `--dry-run`), `"skipped": false`, and the `command` array starting with `/opt/homebrew/bin/openclaw`. **No `"openclaw binary not found"` error.**

- [ ] **Step 7: Optional — verify a real intervention end-to-end**

Trigger a fresh frustration message in OpenClaw (e.g., type something like `测试 phase 0 修复` then a clear frustration signal). Wait ~30s for the autopilot's watch interval, then:

```bash
tail -3 ~/.agent-doctor/openclaw/events.jsonl
ls -la ~/.agent-doctor/openclaw/delivery-errors.jsonl 2>/dev/null
```

Expected: a new line in `events.jsonl` with `"action": "intervene"`, and `delivery-errors.jsonl` either has no new lines OR if it does, the new line contains structured `rc=N stderr=...` instead of the old `returned non-zero exit status 1` string.

---

## Self-review checklist (run after planning, fix inline)

- [x] Spec coverage — Phase 0 in spec maps to Tasks 1-4 here.
- [x] Placeholder scan — no "TBD"/"TODO" in plan; all code shown literally; commands have expected outputs.
- [x] Type consistency — `run_notify_command` signature unchanged (`(command: str, event: AutopilotEvent) -> str | None`); all references match.
- [x] Frequent commits — Task 3 is the single bundled commit; Tasks 1-2 stage but don't commit (intentional, documented).

## Phase 0 done when

- All four tasks above complete with all expected outputs verified.
- `git log -1` shows one Phase 0 commit on `main`.
- `agent-doctor doctor` reports the autopilot service running.
- `~/.agent-doctor/openclaw/delivery-errors.jsonl` does not gain new "exit status 1" lines on subsequent intervene events (or if it does, they contain structured stderr).

## Next phase

After Phase 0 is verified and stable for a session or two, move to Phase 1 (adapter substrate). Phase 1 plan TBD — write it after Phase 0 ships and we have feedback from the live fix.
