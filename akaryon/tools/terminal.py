import subprocess
import time
import os
from pathlib import Path

from akaryon.core.config import Settings, get_settings
from akaryon.tools.base import ToolResult


class TerminalTool:
    name = "terminal"
    description = "Run an allowlisted command without a shell."
    capability = "terminal"
    approval_required_each_time = True
    schema = {"type": "function", "function": {"name": "terminal", "description": description,
              "parameters": {"type": "object", "properties": {
                  "command": {"type": "array", "items": {"type": "string"}},
                  "cwd": {"type": "string"}}, "required": ["command"], "additionalProperties": False}}}
    _environment_allowlist = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC"}

    def __init__(self, settings: Settings | None = None, timeout_seconds: int = 30) -> None:
        cfg = settings or get_settings()
        self.allowed = cfg.allowed_commands
        self.roots = [Path(path).expanduser().resolve() for path in cfg.allowed_paths]
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = cfg.max_terminal_output_bytes

    def resolve_cwd(self, cwd: str | None = None) -> Path | None:
        """Resolve a command directory only inside a configured workspace."""
        roots = [root for root in self.roots if root.exists() and root.is_dir()]
        if not roots:
            return None
        try:
            target = Path(cwd).expanduser().resolve() if cwd else roots[0]
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if not target.exists() or not target.is_dir():
            return None
        if not any(target == root or root in target.parents for root in roots):
            return None
        return target

    def validate_cwd(self, cwd: str | None = None) -> str | None:
        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            return "Working directory must be a path inside a configured workspace"
        if self.resolve_cwd(cwd) is None:
            return "Working directory must be an existing directory inside a configured workspace"
        return None

    def validate(self, command: list[str], cwd: str | None = None) -> str | None:
        if (not isinstance(command, list) or not command or len(command) > 128 or
                any(not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 4096
                    for arg in command)):
            return "Command must be a bounded list of non-empty text arguments"
        if command[0].lower() not in self.allowed:
            return "Command is not on the configured allowlist"
        return self.validate_cwd(cwd)

    def execute(self, command: list[str], cwd: str | None = None) -> ToolResult:
        issue = self.validate(command, cwd)
        if issue:
            return ToolResult(False, error=issue)
        resolved_cwd = self.resolve_cwd(cwd)
        if resolved_cwd is None:
            return ToolResult(False, error="Working directory must be an existing directory inside a configured workspace")
        start = time.monotonic()
        try:
            safe_env = {key: value for key, value in os.environ.items()
                        if key.upper() in self._environment_allowlist}
            result = subprocess.run(command, cwd=str(resolved_cwd), capture_output=True, text=True,
                                    timeout=self.timeout_seconds, shell=False, check=False,
                                    env=safe_env)
            stdout, stdout_truncated = self._limit_output(result.stdout)
            stderr, stderr_truncated = self._limit_output(result.stderr)
            return ToolResult(result.returncode == 0,
                              {"stdout": stdout, "stderr": stderr, "exit_code": result.returncode},
                              metadata={"duration_seconds": time.monotonic() - start,
                                        "stdout_truncated": stdout_truncated,
                                        "stderr_truncated": stderr_truncated})
        except Exception as exc:
            return ToolResult(False, error=str(exc), metadata={"duration_seconds": time.monotonic() - start})

    def _limit_output(self, value: str | None) -> tuple[str, bool]:
        output = value or ""
        encoded = output.encode("utf-8")
        if len(encoded) <= self.max_output_bytes:
            return output, False
        marker = "\n[output truncated]"
        marker_bytes = marker.encode("utf-8")
        if self.max_output_bytes <= len(marker_bytes):
            return encoded[:self.max_output_bytes].decode("utf-8", errors="ignore"), True
        content_limit = self.max_output_bytes - len(marker_bytes)
        clipped = encoded[:content_limit].decode("utf-8", errors="ignore")
        return clipped + marker, True
