"""Executable contract tests. These never invoke a paid model or use credentials."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from auto_outbound.providers import AgentRunner, ProviderError, inspect_providers

SCHEMA = {
    "type": "object",
    "properties": {"company": {"type": "string"}},
    "required": ["company"],
    "additionalProperties": False,
}

FAKE_CLI = r'''
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("codex-cli 0.163.0")
    raise SystemExit(0)
if "--help" in args:
    flags = """--ignore-user-config --ephemeral --skip-git-repo-check --sandbox
    --json --config --strict-config --output-schema --output-last-message
    --safe-mode --output-format --json-schema --tools --allowedTools
    --disallowedTools --strict-mcp-config --mcp-config --permission-mode
    --no-session-persistence --max-turns --max-budget-usd"""
    if os.environ.get("FAKE_OLD"):
        flags = flags.replace("--safe-mode", "").replace("--ignore-user-config", "")
    print(flags)
    raise SystemExit(0)
prompt = sys.stdin.read()
Path(os.environ["FAKE_LOG"]).write_text(json.dumps({"argv": args, "prompt": prompt, "cwd": os.getcwd()}))
mode = os.environ.get("FAKE_MODE", "success")
if mode == "auth":
    print("Authentication failed; private token sk-do-not-expose", file=sys.stderr)
    raise SystemExit(1)
if mode == "rate":
    print("429 quota exhausted", file=sys.stderr)
    raise SystemExit(1)
if mode == "flood":
    sys.stderr.write("private-log" * 100_000)
    sys.stderr.flush()
    time.sleep(5)
if mode == "hang":
    time.sleep(30)
if mode == "child":
    p = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])
    Path(os.environ["FAKE_CHILD_PID"]).write_text(str(p.pid))
    time.sleep(30)
if mode == "malformed":
    print("not JSON")
    raise SystemExit(0)
if args and args[0] == "exec":
    if mode == "event_error":
        print(json.dumps({"type": "turn.failed", "error": {"message": "authentication failed secret"}}))
        raise SystemExit(0)
    if mode != "missing":
        target = Path(args[args.index("--output-last-message") + 1])
        if mode == "large_file":
            target.write_text("x" * 100_000)
        elif mode == "array":
            target.write_text("[]")
        else:
            target.write_text(json.dumps({"company": "Example machine shop"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 34}}))
else:
    envelope = {"type": "result", "subtype": "success", "is_error": False,
                "structured_output": {"company": "Example machine shop"},
                "usage": {"input_tokens": 120, "output_tokens": 34}, "total_cost_usd": 0.004}
    if mode == "missing":
        del envelope["structured_output"]
    if mode == "array":
        envelope["structured_output"] = []
    if mode == "event_error":
        envelope["is_error"] = True
        envelope["subtype"] = "error_max_turns"
    print(json.dumps(envelope))
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip(
            "This executable fixture uses a POSIX shebang; parser tests run on every OS."
        )
    executable = tmp_path / "fake-agent"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_CLI, encoding="utf-8")
    executable.chmod(0o700)
    log = tmp_path / "calls.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("AUTO_OUTBOUND_CODEX_BIN", str(executable))
    monkeypatch.setenv("AUTO_OUTBOUND_CLAUDE_BIN", str(executable))
    return executable, log


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_success_transport_isolated_prompt_and_usage(provider, fake_cli, tmp_path):
    executable, log = fake_cli
    sentinel = tmp_path / "must-not-exist"
    prompt = f'Research this literal input: $(touch "{sentinel}") `whoami`; & < >'
    result = AgentRunner(provider, executable=str(executable), model="test-model").run(
        prompt, SCHEMA
    )
    assert result.data == {"company": "Example machine shop"}
    assert result.input_tokens == 120
    assert result.output_tokens == 34
    assert result.cost_usd == (0.004 if provider == "claude" else None)
    call = json.loads(log.read_text())
    assert call["prompt"] == prompt
    assert (
        prompt not in call["argv"]
    )  # stdin does not expose the prompt through process argv.
    assert not sentinel.exists()
    assert Path(call["cwd"]) != Path.cwd()
    assert not Path(call["cwd"]).exists()  # no lingering prompt/results workspace.
    assert call["argv"][call["argv"].index("--model") + 1] == "test-model"


def test_codex_explicit_permissions_and_web_disabled(fake_cli):
    executable, log = fake_cli
    AgentRunner("codex", executable=str(executable)).run(
        "Draft only", SCHEMA, web=False
    )
    args = json.loads(log.read_text())["argv"]
    assert args[0] == "exec" and args[-1] == "-"
    assert "--ignore-user-config" in args
    assert "--strict-config" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert 'approval_policy="never"' in args
    assert 'web_search="disabled"' in args
    assert "features.shell_tool=false" in args
    assert "features.apps=false" in args
    assert "features.multi_agent=false" in args
    assert "features.plugins=false" in args
    assert "features.hooks=false" in args
    assert not any("dangerous" in arg for arg in args)


@pytest.mark.parametrize(
    "web,expected_tools", [(True, "WebSearch,WebFetch"), (False, "")]
)
def test_claude_restricts_tools_and_inherits_no_extensions(
    fake_cli, web, expected_tools
):
    executable, log = fake_cli
    AgentRunner(
        "claude", executable=str(executable), max_turns=6, max_cost_usd=0.5
    ).run("Research", SCHEMA, web=web)
    args = json.loads(log.read_text())["argv"]
    assert "--safe-mode" in args and "--bare" not in args
    assert args[args.index("--tools") + 1] == expected_tools
    assert args[args.index("--allowedTools") + 1] == expected_tools
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--max-turns") + 1] == "6"
    assert args[args.index("--max-budget-usd") + 1] == "0.5"
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_old_cli_fails_closed_before_model_call(provider, fake_cli, monkeypatch):
    executable, log = fake_cli
    monkeypatch.setenv("FAKE_OLD", "1")
    with pytest.raises(ProviderError, match="Update") as error:
        AgentRunner(provider, executable=str(executable)).run("Research", SCHEMA)
    assert error.value.code == "unsupported_cli"
    assert not log.exists()


def test_inspect_never_runs_a_model(fake_cli):
    _, log = fake_cli
    providers = inspect_providers()
    assert [item["id"] for item in providers] == ["codex", "claude"]
    assert all(item["available"] and item["version"] == "0.163.0" for item in providers)
    assert not log.exists()


def test_missing_executable_is_actionable(monkeypatch):
    monkeypatch.setenv("AUTO_OUTBOUND_CODEX_BIN", "/definitely/not/installed")
    status = inspect_providers()[0]
    assert not status["available"]
    assert status["code"] == "not_installed"
    assert "AUTO_OUTBOUND_CODEX_BIN" in status["message"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    "mode,code", [("auth", "auth_required"), ("rate", "rate_limited")]
)
def test_errors_are_classified_without_private_logs(
    provider, mode, code, fake_cli, monkeypatch
):
    executable, _ = fake_cli
    monkeypatch.setenv("FAKE_MODE", mode)
    with pytest.raises(ProviderError) as error:
        AgentRunner(provider, executable=str(executable)).run("Research", SCHEMA)
    assert error.value.code == code
    assert "sk-do-not-expose" not in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    "mode,code",
    [("malformed", "invalid_response"), ("missing", "missing_result"), ("array", None)],
)
def test_bad_results_rejected(provider, mode, code, fake_cli, monkeypatch):
    executable, _ = fake_cli
    monkeypatch.setenv("FAKE_MODE", mode)
    with pytest.raises(ProviderError) as error:
        AgentRunner(provider, executable=str(executable)).run("Research", SCHEMA)
    assert error.value.code == (
        code or ("invalid_response" if provider == "codex" else "missing_result")
    )


@pytest.mark.parametrize(
    "provider,code", [("codex", "auth_required"), ("claude", "agent_budget")]
)
def test_zero_exit_error_envelope_rejected(provider, code, fake_cli, monkeypatch):
    executable, _ = fake_cli
    monkeypatch.setenv("FAKE_MODE", "event_error")
    with pytest.raises(ProviderError) as error:
        AgentRunner(provider, executable=str(executable)).run("Research", SCHEMA)
    assert error.value.code == code


def test_no_deadlock_when_stderr_exceeds_cap(fake_cli, monkeypatch):
    executable, _ = fake_cli
    monkeypatch.setenv("FAKE_MODE", "flood")
    start = time.monotonic()
    with pytest.raises(ProviderError) as error:
        AgentRunner("codex", executable=str(executable), max_output_bytes=1024).run(
            "Research", SCHEMA
        )
    assert error.value.code == "output_limit"
    assert time.monotonic() - start < 4


def test_result_file_also_has_a_size_cap(fake_cli, monkeypatch):
    executable, _ = fake_cli
    monkeypatch.setenv("FAKE_MODE", "large_file")
    with pytest.raises(ProviderError) as error:
        AgentRunner("codex", executable=str(executable), max_output_bytes=1024).run(
            "Research", SCHEMA
        )
    assert error.value.code == "output_limit"


def test_timeout_does_not_hang(fake_cli, monkeypatch):
    executable, _ = fake_cli
    monkeypatch.setenv("FAKE_MODE", "hang")
    start = time.monotonic()
    with pytest.raises(ProviderError) as error:
        AgentRunner("codex", executable=str(executable), timeout=0.15).run(
            "Research", SCHEMA
        )
    assert error.value.code == "timeout"
    assert time.monotonic() - start < 4


def test_cancellation_before_launch_does_no_work(fake_cli):
    executable, log = fake_cli
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProviderError) as error:
        AgentRunner("codex", executable=str(executable)).run(
            "Research", SCHEMA, cancel=cancel
        )
    assert error.value.code == "cancelled" and not log.exists()


def test_cancellation_terminates_child_processes(fake_cli, tmp_path, monkeypatch):
    executable, _ = fake_cli
    pid_path = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_MODE", "child")
    monkeypatch.setenv("FAKE_CHILD_PID", str(pid_path))
    cancel = threading.Event()

    def cancel_after_start():
        for _ in range(100):
            if pid_path.exists():
                time.sleep(0.1)
                cancel.set()
                return
            time.sleep(0.03)
        cancel.set()

    timer = threading.Thread(target=cancel_after_start, daemon=True)
    timer.start()
    try:
        with pytest.raises(ProviderError) as error:
            AgentRunner("codex", executable=str(executable)).run(
                "Research", SCHEMA, cancel=cancel
            )
        assert error.value.code == "cancelled"
        assert pid_path.exists()
        child_pid = int(pid_path.read_text())
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        # A killed orphan can briefly remain a zombie until PID 1 reaps it.
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child_pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        ).stdout.strip()
        # ps works on both macOS and Linux. Empty means it was reaped between
        # kill(pid, 0) and this status check; Z means killed and awaiting reap.
        assert not status or status.startswith("Z"), "Agent child survived cancellation"
    finally:
        timer.join(timeout=4)
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_nonfinite_or_boolean_usage_is_not_reported():
    response = {
        "structured_output": {"company": "Fixture"},
        "usage": {"input_tokens": True, "output_tokens": -1},
        "total_cost_usd": float("inf"),
    }
    result = AgentRunner("claude")._parse_claude(json.dumps(response).encode())
    assert (
        result.input_tokens is None
        and result.output_tokens is None
        and result.cost_usd is None
    )


def test_unknown_provider_rejected():
    with pytest.raises(ProviderError) as error:
        AgentRunner("invented")
    assert error.value.code == "unsupported_provider"


def test_no_command_wrapper_fallback(tmp_path, monkeypatch):
    wrapper = tmp_path / "claude.cmd"
    wrapper.write_text("echo unsafe")
    wrapper.chmod(0o700)
    monkeypatch.setenv("AUTO_OUTBOUND_CLAUDE_BIN", str(wrapper))
    status = inspect_providers()[1]
    assert status["code"] == "native_executable_required"
