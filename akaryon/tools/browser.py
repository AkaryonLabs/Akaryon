"""Explicitly approved external browser navigation."""

from urllib.parse import urlsplit
import webbrowser

from akaryon.tools.base import ToolResult


class OpenBrowserTool:
    name = "open_browser"
    description = "Open a user-approved HTTP or HTTPS URL in the default browser."
    capability = "external_browser"
    approval_required_each_time = True
    schema = {"type": "function", "function": {"name": name, "description": description,
              "parameters": {"type": "object", "properties": {
                  "url": {"type": "string", "maxLength": 2048}},
                  "required": ["url"], "additionalProperties": False}}}

    @staticmethod
    def validate(url: str) -> str | None:
        if not isinstance(url, str) or not url or len(url) > 2048:
            return "URL must be a non-empty string no longer than 2048 characters"
        try:
            parsed = urlsplit(url)
            # Force validation of malformed ports and require an unambiguous web URL.
            _ = parsed.port
        except ValueError:
            return "URL is malformed"
        if parsed.scheme.lower() not in {"http", "https"}:
            return "Only HTTP and HTTPS URLs can be opened"
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return "URL must include a host and cannot contain embedded credentials"
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            return "URL cannot contain control characters"
        return None

    def execute(self, url: str) -> ToolResult:
        issue = self.validate(url)
        if issue:
            return ToolResult(False, error=issue)
        try:
            opened = webbrowser.open(url, new=2, autoraise=True)
        except Exception:
            return ToolResult(False, error="Could not open the default browser")
        if not opened:
            return ToolResult(False, error="The default browser did not accept the URL")
        parsed = urlsplit(url)
        return ToolResult(True, output={"opened": True, "host": parsed.hostname})
