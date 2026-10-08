from akaryon.memory.manager import MemoryManager
from akaryon.memory.security import contains_credential_material
from akaryon.tools.base import ToolResult


class MemoryTool:
    """Store an explicitly requested note in Akaryon's built-in memory database."""

    name = "memory_store"
    description = (
        "Save a durable note to Akaryon's built-in memory database. This never reads or writes files. "
        "Only use when the user explicitly asks you to remember or save something. "
        "Never store passwords, API keys, access tokens, or other credentials."
    )
    capability = "memory"
    schema = {"type": "function", "function": {"name": name, "description": description,
              "parameters": {"type": "object", "properties": {
                  "title": {"type": "string", "minLength": 1, "maxLength": 200},
                  "content": {"type": "string", "minLength": 1, "maxLength": 50000},
                  "scope": {"type": "string", "enum": ["long_term", "project", "document"]},
                  "project_id": {"type": "string", "maxLength": 100}},
                  "required": ["title", "content"], "additionalProperties": False}}}

    def __init__(self, memory: MemoryManager) -> None:
        self.memory = memory

    def validate(self, title: str, content: str, scope: str = "long_term",
                 project_id: str | None = None) -> str | None:
        title, content = title.strip(), content.strip()
        if not title or len(title) > 200:
            return "Memory title must contain 1 to 200 characters"
        if not content or len(content) > 50000:
            return "Memory content must contain 1 to 50,000 characters"
        if scope not in {"long_term", "project", "document"}:
            return "Unsupported memory scope"
        if scope == "project" and not project_id:
            return "A project ID is required for project memory"
        if scope != "project" and project_id:
            return "A project ID can only be used with project memory"
        if contains_credential_material(title, content):
            return "Memory cannot store credentials or private keys"
        return None

    def execute(self, title: str, content: str, scope: str = "long_term",
                project_id: str | None = None) -> ToolResult:
        issue = self.validate(title, content, scope, project_id)
        if issue:
            return ToolResult(False, error=issue)
        try:
            entry = self.memory.store_entry(title.strip(), content.strip(), scope, project_id,
                                            {"source": "assistant_tool"})
        except ValueError as exc:
            return ToolResult(False, error=str(exc))
        return ToolResult(True, {"id": entry.id, "title": entry.title,
                                "scope": entry.scope, "project_id": entry.project_id})
