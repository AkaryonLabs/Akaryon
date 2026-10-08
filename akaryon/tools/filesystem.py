from itertools import islice
from pathlib import Path
from typing import Any

from akaryon.core.config import Settings, get_settings
from akaryon.tools.base import ToolResult


class FilesystemTool:
    name = "filesystem"
    description = "Read, write, list, or create directories under configured roots."
    capability = "filesystem"
    schema = {"type": "function", "function": {"name": "filesystem", "description": description,
              "parameters": {"type": "object", "properties": {
                  "operation": {"type": "string", "enum": ["read", "write", "list", "mkdir"]},
                  "path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["operation", "path"], "additionalProperties": False}}}

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        self.roots = [Path(p).resolve() for p in cfg.allowed_paths]
        self.max_file_bytes = cfg.max_filesystem_file_bytes
        self.max_directory_entries = cfg.max_directory_entries

    def _path(self, value: str) -> Path:
        path = Path(value).resolve()
        if not any(path == root or root in path.parents for root in self.roots):
            raise PermissionError("Path is outside configured allowed directories")
        return path

    def validate(self, operation: str, path: str, content: str | None = None) -> str | None:
        if operation not in {"read", "write", "list", "mkdir"}:
            return "Unsupported filesystem operation"
        try:
            target = self._path(path)
        except Exception:
            return "Path is outside configured allowed directories or invalid"
        if operation == "write":
            if content is None:
                return "content is required for a write"
            if len(content.encode("utf-8")) > self.max_file_bytes:
                return f"Content exceeds the {self.max_file_bytes}-byte write limit"
            if target.exists() and target.is_dir():
                return "Write target is a directory"
        elif operation == "read" and (not target.exists() or not target.is_file()):
            return "Read target must be an existing file"
        elif operation == "list" and (not target.exists() or not target.is_dir()):
            return "List target must be an existing directory"
        elif operation == "mkdir" and target.exists() and not target.is_dir():
            return "Directory target is an existing file"
        return None

    def execute(self, operation: str, path: str, content: str | None = None) -> ToolResult:
        try:
            issue = self.validate(operation, path, content)
            if issue:
                return ToolResult(False, error=issue)
            target = self._path(path)
            if operation == "read":
                with target.open("rb") as file:
                    content_bytes = file.read(self.max_file_bytes + 1)
                if len(content_bytes) > self.max_file_bytes:
                    return ToolResult(False, error=f"File exceeds the {self.max_file_bytes}-byte read limit")
                return ToolResult(True, content_bytes.decode("utf-8"))
            if operation == "write":
                if content is None:
                    return ToolResult(False, error="content is required")
                content_bytes = content.encode("utf-8")
                if len(content_bytes) > self.max_file_bytes:
                    return ToolResult(False, error=f"Content exceeds the {self.max_file_bytes}-byte write limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content_bytes)
                return ToolResult(True, str(target))
            if operation == "list":
                entries = list(islice(target.iterdir(), self.max_directory_entries + 1))
                truncated = len(entries) > self.max_directory_entries
                names = [entry.name for entry in entries[:self.max_directory_entries]]
                return ToolResult(True, names, metadata={"truncated": truncated})
            if operation == "mkdir":
                target.mkdir(parents=True, exist_ok=True)
                return ToolResult(True, str(target))
            return ToolResult(False, error=f"Unsupported operation: {operation}")
        except Exception as exc:
            return ToolResult(False, error=str(exc))
