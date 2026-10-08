from akaryon.tools.base import ToolResult
from akaryon.tools.terminal import TerminalTool


class GitTool:
    name = "git"
    description = "Inspect and make local Git changes; never pushes to remotes."
    capability = "git"
    schema = {"type": "function", "function": {"name": "git", "description": description,
              "parameters": {"type": "object", "properties": {
                  "operation": {"type": "string", "enum": ["status", "diff", "log", "branch", "add", "commit"]},
                  "repo": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}},
                  "message": {"type": "string"}}, "required": ["operation", "repo"],
                  "additionalProperties": False}}}
    _commands = {"status": ["status", "--short", "--branch"], "diff": ["diff"],
                 "log": ["log", "-10", "--oneline"], "branch": ["branch", "--show-current"],
                 "add": ["add"], "commit": ["commit"]}

    def __init__(self, terminal: TerminalTool) -> None:
        self.terminal = terminal

    @staticmethod
    def approval_required_each_time(arguments: dict) -> bool:
        return arguments.get("operation") in {"add", "commit"}

    def validate(self, operation: str, repo: str, paths: list[str] | None = None,
                 message: str | None = None) -> str | None:
        if operation not in self._commands:
            return "Unsupported or remote Git operation"
        if not isinstance(repo, str) or not repo.strip():
            return "Repository must be an existing directory inside a configured workspace"
        if operation == "add" and (not isinstance(paths, list) or not paths or
                                     any(not isinstance(path, str) or not path or "\x00" in path
                                         for path in paths)):
            return "Specify valid paths to add"
        if operation == "commit" and (not isinstance(message, str) or not message.strip() or
                                        "\x00" in message):
            return "Commit message is required"
        validate_cwd = getattr(self.terminal, "validate_cwd", None)
        if validate_cwd:
            return validate_cwd(repo)
        return None

    def execute(self, operation: str, repo: str, paths: list[str] | None = None,
                message: str | None = None) -> ToolResult:
        issue = self.validate(operation, repo, paths, message)
        if issue:
            return ToolResult(False, error=issue)
        args = self._commands[operation].copy()
        if operation == "add":
            if not paths:
                return ToolResult(False, error="Specify paths to add")
            args.extend(["--", *paths])
        if operation == "commit":
            if not message:
                return ToolResult(False, error="Commit message is required")
            args += ["-m", message]
        return self.terminal.execute(["git", *args], cwd=repo)
