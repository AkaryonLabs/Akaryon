"""Read-only Codex CLI adapter for Akaryon agent runs."""

from __future__ import annotations

from collections.abc import Iterator
from queue import Empty, Queue
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

from akaryon.agents.base import AgentActionProposal, AgentBackendEvent, AgentBackendRequest


_END = object()
_ACTION_PROPOSAL_PREFIX = "AKARYON_ACTION_PROPOSAL\n"
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|ACCESS_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|AUTH|"
    r"DATABASE_URL|DB_URL|DSN)(?:_|$)",
    re.IGNORECASE,
)
_POLICY_BLOCKED_COMMAND = re.compile(
    r"\b(?:shell|command|exec_command)\b.{0,120}\bblocked by policy\b|"
    r"\bblocked by policy\b.{0,120}\b(?:shell|command|exec_command)\b",
    re.IGNORECASE | re.DOTALL,
)
_HEALTH_CACHE_SECONDS = 30


def _child_environment() -> dict[str, str]:
    """Keep runtime credentials out of Codex-launched command environments."""
    return {name: value for name, value in os.environ.items()
            if not _SENSITIVE_ENV_NAME.search(name)}


def _reject_json_constant(value: str):
    raise ValueError(f"Unsupported JSON constant: {value}")


class CodexCliBackend:
    """Run Codex CLI in JSON mode with a read-only workspace sandbox.

    This adapter deliberately grants no filesystem or terminal capability to
    the child. It is an inspect-and-advise backend until a host-mediated action
    protocol is integrated; all changes remain in Akaryon's existing tools.
    """

    backend_id = "codex_cli"

    def __init__(self, command: str = "codex") -> None:
        self.command = command
        self._active: dict[str, CodexCliRun] = {}
        self._active_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._health_state_lock = threading.Lock()
        self._health_checked_at: float | None = None
        self._health_result = False
        self._health_checking = False
        self._health_status = "unchecked"

    def health_snapshot(self) -> dict[str, bool]:
        """Return cached CLI readiness and refresh it off the request thread."""
        with self._health_state_lock:
            now = time.monotonic()
            if (self._health_checked_at is None or
                    now - self._health_checked_at >= _HEALTH_CACHE_SECONDS) and not self._health_checking:
                self._health_checking = True
                threading.Thread(target=self._refresh_health_snapshot, daemon=True,
                                 name="akaryon-codex-health").start()
            status = "checking" if self._health_checking else self._health_status
            return {"available": self._health_result, "checking": self._health_checking,
                    "status": status}

    def _refresh_health_snapshot(self) -> None:
        try:
            result = self.health()
        except Exception:
            result = False
            with self._health_state_lock:
                self._health_status = "unavailable"
        with self._health_state_lock:
            self._health_result = result
            if self._health_status in {"unchecked", "checking"}:
                self._health_status = "ready" if result else "unavailable"
            self._health_checked_at = time.monotonic()
            self._health_checking = False

    def health(self) -> bool:
        with self._health_lock:
            with self._health_state_lock:
                now = time.monotonic()
                if (self._health_checked_at is not None and
                        now - self._health_checked_at < _HEALTH_CACHE_SECONDS):
                    return self._health_result
            result = self._run_health_probes()
            with self._health_state_lock:
                self._health_result = result
                self._health_checked_at = time.monotonic()
            return result

    def _run_health_probes(self) -> bool:
        command = self._resolve_command()
        if command is None:
            self._set_health_status("not_installed")
            return False
        if not self._probe([*command, "--version"]):
            self._set_health_status("cli_failed")
            return False
        if not self._probe([*command, "exec", "--ignore-user-config", "--help"]):
            self._set_health_status("unsupported")
            return False
        if os.name == "nt":
            whoami = shutil.which("whoami.exe")
            if not whoami or not self._probe([*command, "sandbox", "--", whoami],
                                            timeout=10):
                self._set_health_status("sandbox_unavailable")
                return False
        if not self._probe([*command, "login", "status"]):
            self._set_health_status("not_signed_in")
            return False
        self._set_health_status("ready")
        return True

    def _set_health_status(self, status: str) -> None:
        with self._health_state_lock:
            self._health_status = status

    @staticmethod
    def _probe(argv: list[str], *, timeout: int = 5) -> bool:
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=_child_environment(), shell=False, timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError, ValueError):
            return False

    def start(self, request: AgentBackendRequest) -> "CodexCliRun":
        command = self._resolve_command()
        if command is None:
            raise RuntimeError("Codex CLI is not installed or is not available on PATH")
        argv = [*command, "exec", "--json", "--ephemeral", "--sandbox", "read-only",
                "--ignore-user-config", "--skip-git-repo-check", request.prompt]
        if request.model:
            argv[-1:-1] = ["--model", request.model]
        try:
            process = subprocess.Popen(
                argv,
                cwd=request.workspace_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=_child_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("Codex CLI could not be started") from exc
        run = CodexCliRun(process, request.timeout_seconds, request.max_output_bytes,
                          on_finish=lambda: self._discard(request.task_id))
        with self._active_lock:
            self._active[request.task_id] = run
        return run

    def cancel_task(self, task_id: str) -> None:
        with self._active_lock:
            run = self._active.get(task_id)
        if run:
            run.cancel()

    def _discard(self, task_id: str) -> None:
        with self._active_lock:
            self._active.pop(task_id, None)

    def _resolve_command(self) -> list[str] | None:
        resolved = shutil.which(self.command)
        if not resolved:
            return None
        path = Path(resolved)
        if os.name == "nt" and path.suffix.casefold() in {".cmd", ".bat"}:
            # npm installs both a cmd shim and a PowerShell shim. Invoking the
            # latter with an argument vector avoids passing user prompts through
            # cmd.exe's metacharacter parser.
            ps1_path = path.with_suffix(".ps1")
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if ps1_path.is_file() and powershell:
                return [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ps1_path)]
        if path.suffix.casefold() == ".ps1":
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            return ([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(path)]
                    if powershell else None)
        return [resolved]


class CodexCliRun:
    def __init__(self, process: subprocess.Popen, timeout_seconds: int,
                 max_output_bytes: int, on_finish=None) -> None:
        self.process = process
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.run_id = str(uuid.uuid4())
        self._cancelled = False
        self._started_at = time.monotonic()
        self._on_finish = on_finish
        self._queue: Queue[bytes | object] = Queue(maxsize=16)
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._discard_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in iter(self.process.stdout.readline, b""):
                self._queue.put(line)
        finally:
            self._queue.put(_END)

    def _discard_stderr(self) -> None:
        assert self.process.stderr is not None
        while self.process.stderr.read(8192):
            pass

    def events(self) -> Iterator[AgentBackendEvent]:
        try:
            if self._cancelled:
                yield AgentBackendEvent("cancelled")
                return
            yield from self._read_events()
        finally:
            if self.process.poll() is None:
                self.cancel()
            if self._on_finish:
                self._on_finish()

    def _read_events(self) -> Iterator[AgentBackendEvent]:
        total_bytes = 0
        saw_failure = False
        invalid_event = False
        policy_blocked = False
        invalid_proposal = False
        proposed_action = None
        while True:
            remaining = self.timeout_seconds - (time.monotonic() - self._started_at)
            if remaining <= 0:
                self.cancel()
                yield AgentBackendEvent("failed", error_code="codex_timeout")
                return
            try:
                line = self._queue.get(timeout=min(0.1, remaining))
            except Empty:
                if self.process.poll() is not None:
                    break
                continue
            if line is _END:
                break
            total_bytes += len(line)
            if total_bytes > self.max_output_bytes:
                self.cancel()
                yield AgentBackendEvent("failed", error_code="codex_output_limit")
                return
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.cancel()
                yield AgentBackendEvent("failed", error_code="codex_invalid_jsonl")
                return
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                invalid_event = True
                continue
            event_type = event["type"]
            if event_type == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    invalid_event = True
                    continue
                if item["type"] == "agent_message":
                    text = item.get("text")
                    if not isinstance(text, str):
                        invalid_event = True
                        continue
                    if text:
                        if _POLICY_BLOCKED_COMMAND.search(text):
                            policy_blocked = True
                        elif text.startswith(_ACTION_PROPOSAL_PREFIX):
                            try:
                                data = json.loads(text[len(_ACTION_PROPOSAL_PREFIX):],
                                                  parse_constant=_reject_json_constant)
                                if not isinstance(data, dict) or set(data) != {"tool", "arguments"}:
                                    raise ValueError("invalid proposal fields")
                                if not isinstance(data["tool"], str) or not isinstance(data["arguments"], dict):
                                    raise ValueError("invalid proposal types")
                                proposal = AgentActionProposal(data["tool"], data["arguments"])
                                if proposed_action is not None:
                                    raise ValueError("multiple proposals")
                                proposed_action = proposal
                            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                                invalid_proposal = True
                        elif proposed_action is None:
                            yield AgentBackendEvent("text", text=text)
            elif event_type == "turn.failed" or event_type == "error":
                saw_failure = True
        try:
            exit_code = self.process.wait(timeout=max(0.0, self.timeout_seconds -
                                                       (time.monotonic() - self._started_at)))
        except subprocess.TimeoutExpired:
            self.cancel()
            yield AgentBackendEvent("failed", error_code="codex_timeout")
            return
        if self._cancelled:
            yield AgentBackendEvent("cancelled")
        elif exit_code != 0 or saw_failure or invalid_event or invalid_proposal or policy_blocked:
            if invalid_proposal:
                code = "codex_invalid_proposal"
            elif policy_blocked:
                code = "codex_policy_blocked"
            elif invalid_event:
                code = "codex_invalid_event"
            else:
                code = "codex_turn_failed"
            yield AgentBackendEvent("failed", error_code=code)
        elif proposed_action is not None:
            yield AgentBackendEvent("action_requested", action=proposed_action)
        else:
            yield AgentBackendEvent("completed")

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        if self.process.poll() is None:
            if os.name == "nt":
                # The npm PowerShell shim may own a Node child. Terminating only
                # the shim can leave Codex running with stdout pipes held open.
                taskkill = shutil.which("taskkill")
                if taskkill:
                    try:
                        subprocess.run(
                            [taskkill, "/PID", str(self.process.pid), "/T", "/F"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, shell=False, timeout=4,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except (OSError, subprocess.SubprocessError, ValueError):
                        pass
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
