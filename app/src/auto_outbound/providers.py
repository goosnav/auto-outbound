"""Bounded adapters for a user's installed Codex or Claude Code CLI.

The adapter owns process isolation and transport, not the campaign policy. The
application validates returned data with its Pydantic models before saving it.
No command is executed through a shell and no provider credentials are copied.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentResult:
    data: dict[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class ProviderError(Exception):
    """Stable, safe-to-display failure; never includes raw provider output."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


_REQUIRED_FLAGS = {
    "codex": (
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "--json",
        "--config",
        "--strict-config",
        "--output-schema",
        "--output-last-message",
    ),
    "claude": (
        "--safe-mode",
        "--output-format",
        "--json-schema",
        "--tools",
        "--allowedTools",
        "--disallowedTools",
        "--strict-mcp-config",
        "--mcp-config",
        "--permission-mode",
        "--no-session-persistence",
        "--max-turns",
        "--max-budget-usd",
    ),
}


def _resolve_executable(provider: str, executable: str | None = None) -> str:
    if provider not in _REQUIRED_FLAGS:
        raise ProviderError("unsupported_provider", "Choose Codex or Claude Code.")
    candidate = executable or os.getenv(f"AUTO_OUTBOUND_{provider.upper()}_BIN") or provider
    found = shutil.which(candidate)
    if not found:
        raise ProviderError(
            "not_installed",
            f"Install {provider} and add it to PATH, or set AUTO_OUTBOUND_{provider.upper()}_BIN "
            "to its executable. Then run the provider check again.",
        )
    # Windows can implicitly invoke a shell for batch files despite shell=False.
    # A native CLI executable prevents schema/prompt arguments reaching cmd.exe.
    if Path(found).suffix.lower() in {".cmd", ".bat"}:
        raise ProviderError(
            "native_executable_required",
            f"Use the native {provider} executable, not its .cmd/.bat wrapper. "
            f"Set AUTO_OUTBOUND_{provider.upper()}_BIN to the .exe file.",
        )
    return str(Path(found).resolve())


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate this run's children, including a CLI blocked on a tool."""
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        # Every call creates a new session. Never address the parent's group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            pass
        # Also kill descendants if the group leader exited after SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _execute(
    argv: list[str],
    *,
    cwd: str,
    stdin: str = "",
    timeout: float,
    max_bytes: int,
    cancel: threading.Event | None = None,
    result_path: Path | None = None,
) -> tuple[int, bytes, bytes]:
    """Drain both pipes concurrently; cap their combined in-memory allocation."""
    if cancel and cancel.is_set():
        raise ProviderError("cancelled", "Agent run cancelled.")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
    except OSError as exc:
        raise ProviderError(
            "launch_failed", "Could not start the agent. Check its executable and permissions."
        ) from exc

    buffers = [bytearray(), bytearray()]
    lock = threading.Lock()
    over_limit = threading.Event()
    total_bytes = 0

    def read_pipe(index: int) -> None:
        nonlocal total_bytes
        pipe = process.stdout if index == 0 else process.stderr
        assert pipe is not None
        try:
            while chunk := pipe.read(16384):
                with lock:
                    room = max(0, max_bytes - total_bytes)
                    buffers[index].extend(chunk[:room])
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        over_limit.set()
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()

    def write_prompt() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(stdin.encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    readers = [threading.Thread(target=read_pipe, args=(i,), daemon=True) for i in (0, 1)]
    writer = threading.Thread(target=write_prompt, daemon=True)
    for thread in [*readers, writer]:
        thread.start()
    deadline = time.monotonic() + timeout
    error: ProviderError | None = None
    try:
        while process.poll() is None:
            if cancel and cancel.is_set():
                error = ProviderError("cancelled", "Agent run cancelled.")
                break
            if time.monotonic() >= deadline:
                error = ProviderError(
                    "timeout",
                    "Agent time limit reached. Progress is saved; resume to retry this item.",
                )
                break
            file_too_large = (
                result_path is not None
                and result_path.exists()
                and result_path.stat().st_size > max_bytes
            )
            if over_limit.is_set() or file_too_large:
                error = ProviderError("output_limit", "Agent output exceeded the size limit.")
                break
            time.sleep(0.04)
    except BaseException:
        _terminate_tree(process)
        raise
    finally:
        if process.poll() is None:
            _terminate_tree(process)
        for thread in [*readers, writer]:
            thread.join(timeout=0.5)
        if any(thread.is_alive() for thread in readers):
            # A descendant may still hold a pipe after its parent exits.
            _terminate_tree(process)
            for thread in readers:
                thread.join(timeout=0.5)
    if error:
        raise error
    if over_limit.is_set():
        raise ProviderError("output_limit", "Agent output exceeded the size limit.")
    if cancel and cancel.is_set():
        raise ProviderError("cancelled", "Agent run cancelled.")
    return process.returncode or 0, bytes(buffers[0]), bytes(buffers[1])


def _probe(provider: str, executable: str) -> str:
    with tempfile.TemporaryDirectory(prefix="auto-outbound-check-") as directory:
        code, out, _ = _execute(
            [executable, "--version"], cwd=directory, timeout=8, max_bytes=128_000
        )
        version_match = re.search(
            r"\b\d+\.\d+\.\d+(?:[-+][\w.]+)?\b", out.decode("utf-8", "replace")
        )
        version = version_match.group(0) if version_match else "unknown"
        commands = [[executable, "--help"]]
        if provider == "codex":
            commands.append([executable, "exec", "--help"])
        help_text = ""
        for command in commands:
            status, stdout, stderr = _execute(command, cwd=directory, timeout=8, max_bytes=256_000)
            if status:
                raise ProviderError(
                    "unsupported_cli", f"Update {provider}; its help command failed."
                )
            help_text += (stdout + stderr).decode("utf-8", "replace")
        # Newer Claude versions may spell these options with hyphens.
        help_text = help_text.replace("--allowed-tools", "--allowedTools").replace(
            "--disallowed-tools", "--disallowedTools"
        )
        if code or any(
            not re.search(re.escape(flag) + r"(?=[\s,=]|$)", help_text)
            for flag in _REQUIRED_FLAGS[provider]
        ):
            raise ProviderError(
                "unsupported_cli",
                f"Update {provider}. This installation lacks the safe, structured-output flags "
                "Auto Outbound needs. Then run the provider check again.",
            )
        return version


def inspect_providers() -> list[dict[str, Any]]:
    """Check installations and capabilities without invoking a model or reading credentials."""
    result = []
    for provider in _REQUIRED_FLAGS:
        try:
            version = _probe(provider, _resolve_executable(provider))
            result.append(
                {
                    "id": provider,
                    "available": True,
                    "version": version,
                    "message": "CLI ready. Sign-in is checked when a run starts.",
                }
            )
        except ProviderError as exc:
            result.append(
                {
                    "id": provider,
                    "available": False,
                    "version": None,
                    "message": exc.message,
                    "code": exc.code,
                }
            )
    return result


def _provider_failure(provider: str, output: bytes, exit_code: int = 0) -> ProviderError:
    # Classification can inspect output; nothing from it is exposed to the user.
    lowered = output.decode("utf-8", "replace").lower()
    if any(
        term in lowered
        for term in (
            "not logged",
            "unauthorized",
            "authentication",
            "api key",
            "401",
            "login required",
            "please log in",
        )
    ):
        command = "codex login" if provider == "codex" else "claude auth login"
        return ProviderError(
            "auth_required", f"Sign in with `{command}`, then resume the campaign."
        )
    if any(term in lowered for term in ("rate limit", "usage limit", "quota", "429")):
        return ProviderError(
            "rate_limited", "Agent usage limit reached. Wait for your limit to reset, then resume."
        )
    if any(
        term in lowered
        for term in ("max_turns", "max turns", "max_budget", "max budget", "budget limit")
    ):
        return ProviderError(
            "agent_budget",
            "Agent turn or cost limit reached. Increase the limit or narrow the request, then resume.",
        )
    if any(
        term in lowered
        for term in (
            "unknown option",
            "unexpected argument",
            "unrecognized argument",
            "unknown feature",
            "unknown field",
        )
    ):
        return ProviderError(
            "unsupported_cli", f"Update {provider}, then run the provider check again."
        )
    if any(term in lowered for term in ("connection", "network", "timed out", "dns", "tls")):
        return ProviderError(
            "network_error",
            "The agent could not reach its service. Check the connection, then resume.",
        )
    return ProviderError(
        "provider_failed",
        f"{provider.title()} could not complete this item"
        + (f" (exit {exit_code})" if exit_code else "")
        + ". Check the CLI sign-in and service status, then resume.",
    )


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if not (-float("inf") < value < float("inf")):
        return None
    return int(value) if integer else float(value)


class AgentRunner:
    def __init__(
        self,
        provider: str,
        model: str = "",
        timeout: int = 180,
        max_turns: int = 8,
        max_cost_usd: float = 1.0,
        max_output_bytes: int = 2_000_000,
        executable: str | None = None,
    ):
        if provider not in _REQUIRED_FLAGS:
            raise ProviderError("unsupported_provider", "Choose Codex or Claude Code.")
        if (
            not math.isfinite(timeout)
            or not math.isfinite(max_cost_usd)
            or timeout <= 0
            or max_turns < 1
            or max_cost_usd <= 0
            or max_output_bytes < 1024
        ):
            raise ProviderError("invalid_limits", "Agent limits must be positive.")
        self.provider = provider
        self.model = model
        self.timeout = timeout
        self.max_turns = max_turns
        self.max_cost_usd = max_cost_usd
        self.max_output_bytes = max_output_bytes
        self.executable = executable
        self._checked_executable: str | None = None
        self._probe_lock = threading.Lock()

    def run(
        self,
        prompt: str,
        schema: dict,
        *,
        web: bool = True,
        cancel: threading.Event | None = None,
    ) -> AgentResult:
        if len(prompt.encode("utf-8")) > 400_000:
            raise ProviderError(
                "input_limit", "The agent input is too large. Reduce the source material."
            )
        if cancel and cancel.is_set():
            raise ProviderError("cancelled", "Agent run cancelled.")
        executable = _resolve_executable(self.provider, self.executable)
        with self._probe_lock:
            if self._checked_executable != executable:
                _probe(self.provider, executable)
                self._checked_executable = executable
        with tempfile.TemporaryDirectory(prefix="auto-outbound-agent-") as directory:
            schema_path = Path(directory) / "schema.json"
            result_path = Path(directory) / "result.json"
            schema_json = json.dumps(schema, ensure_ascii=False, allow_nan=False)
            if len(schema_json.encode("utf-8")) > 100_000:
                raise ProviderError("input_limit", "The output schema is too large.")
            if self.provider == "codex":
                schema_path.write_text(schema_json, encoding="utf-8")
                argv = [
                    executable,
                    "exec",
                    "--ignore-user-config",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--json",
                    "--strict-config",
                    "--color",
                    "never",
                    "-c",
                    'approval_policy="never"',
                    "-c",
                    f'web_search="{"live" if web else "disabled"}"',
                    "-c",
                    "features.shell_tool=false",
                    "-c",
                    "features.apps=false",
                    "-c",
                    "features.multi_agent=false",
                    "-c",
                    "features.plugins=false",
                    "-c",
                    "features.hooks=false",
                    "-c",
                    "features.memories=false",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(result_path),
                ]
                if self.model:
                    argv += ["--model", self.model]
                argv += ["-"]
            else:
                permitted_tools = "WebSearch,WebFetch" if web else ""
                argv = [
                    executable,
                    "--safe-mode",
                    "-p",
                    "--output-format",
                    "json",
                    "--json-schema",
                    schema_json,
                    "--tools",
                    permitted_tools,
                    "--allowedTools",
                    permitted_tools,
                    "--disallowedTools",
                    "mcp__*",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    "--permission-mode",
                    "dontAsk",
                    "--no-session-persistence",
                    "--max-turns",
                    str(self.max_turns),
                    "--max-budget-usd",
                    str(self.max_cost_usd),
                ]
                if self.model:
                    argv += ["--model", self.model]
            code, stdout, stderr = _execute(
                argv,
                cwd=directory,
                stdin=prompt,
                timeout=self.timeout,
                max_bytes=self.max_output_bytes,
                cancel=cancel,
                result_path=result_path,
            )
            if code:
                raise _provider_failure(self.provider, stdout + stderr, code)
            if self.provider == "codex":
                return self._parse_codex(stdout, result_path)
            return self._parse_claude(stdout)

    def _parse_codex(self, stdout: bytes, result_path: Path) -> AgentResult:
        usage: dict[str, Any] = {}
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ProviderError(
                    "invalid_response", "Codex returned an invalid event stream."
                ) from exc
            if not isinstance(event, dict):
                raise ProviderError("invalid_response", "Codex returned an invalid event stream.")
            if event.get("type") in {"error", "turn.failed"}:
                raise _provider_failure("codex", line)
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
        if not result_path.is_file():
            raise ProviderError(
                "missing_result", "Codex returned no structured result. Resume to retry this item."
            )
        if result_path.stat().st_size > self.max_output_bytes:
            raise ProviderError("output_limit", "Agent output exceeded the size limit.")
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            raise ProviderError(
                "invalid_response", "Codex returned invalid structured data."
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError("invalid_response", "The agent result must be a JSON object.")
        return AgentResult(
            data,
            input_tokens=_number(usage.get("input_tokens"), integer=True),
            output_tokens=_number(usage.get("output_tokens"), integer=True),
        )

    def _parse_claude(self, stdout: bytes) -> AgentResult:
        try:
            envelope = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(
                "invalid_response", "Claude returned invalid structured data."
            ) from exc
        if not isinstance(envelope, dict):
            raise ProviderError("invalid_response", "Claude returned an invalid result envelope.")
        if envelope.get("is_error") or str(envelope.get("subtype", "")).startswith("error"):
            raise _provider_failure("claude", stdout)
        data = envelope.get("structured_output")
        if not isinstance(data, dict):
            raise ProviderError(
                "missing_result", "Claude returned no structured result. Resume to retry this item."
            )
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        return AgentResult(
            data,
            input_tokens=_number(usage.get("input_tokens"), integer=True),
            output_tokens=_number(usage.get("output_tokens"), integer=True),
            cost_usd=_number(envelope.get("total_cost_usd")),
        )
