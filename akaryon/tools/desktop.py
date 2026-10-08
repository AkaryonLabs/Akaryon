"""Allowlisted Windows application windows behind per-action approval."""

from __future__ import annotations

import ctypes
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid

from akaryon.tools.base import ToolResult


_ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_SECRET_ENV = re.compile(
    r"(?:^|_)(?:API_KEY|ACCESS_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|AUTH|"
    r"DATABASE_URL|DB_URL|DSN)(?:_|$)", re.IGNORECASE,
)


class WindowsDesktopTool:
    """Open allowlisted Windows apps and request graceful close of tracked apps.

    The tool never selects an arbitrary PID and never terminates a process. An
    app must be started by this tool in the current Akaryon process before it
    can receive a WM_CLOSE request. Each open and close remains per-call gated.
    """

    name = "windows_app"
    description = (
        "Open an explicitly configured Windows app or request a graceful close "
        "for a window opened by Akaryon. Opening requires an allowlisted alias. "
        "Closing only sends WM_CLOSE to the exact tracked launch; it never force "
        "terminates an app. A save prompt may remain visible. Each action requires approval."
    )
    capability = "desktop_window"
    approval_required_each_time = True
    schema = {"type": "function", "function": {"name": name, "description": description,
              "parameters": {"type": "object", "properties": {
                  "operation": {"type": "string", "enum": ["open", "close"]},
                  "application": {"type": "string", "maxLength": 48,
                                  "description": "Configured app alias; required for open."},
                  "launch_id": {"type": "string", "maxLength": 36,
                                "description": "ID returned by windows_app open; required for close."},
              }, "required": ["operation"], "additionalProperties": False}}}

    def __init__(self, applications_json: str = "{}") -> None:
        try:
            raw = json.loads(applications_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("AKARYON_DESKTOP_APPS_JSON must be a JSON object") from exc
        if not isinstance(raw, dict) or len(raw) > 32:
            raise ValueError("AKARYON_DESKTOP_APPS_JSON must contain at most 32 app aliases")
        self._applications: dict[str, str] = {}
        for alias, executable in raw.items():
            if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
                raise ValueError("Desktop app aliases must use lowercase letters, digits, _ or -")
            if not isinstance(executable, str) or not executable or len(executable) > 2048:
                raise ValueError(f"Desktop app '{alias}' must have an executable path")
            path = Path(executable).expanduser()
            if not path.is_absolute() or path.suffix.casefold() != ".exe":
                raise ValueError(f"Desktop app '{alias}' must map to an absolute .exe path")
            self._applications[alias] = str(path.resolve())
        self.schema = deepcopy(type(self).schema)
        app_schema = self.schema["function"]["parameters"]["properties"]["application"]
        if self._applications:
            app_schema["enum"] = sorted(self._applications)
            self.schema["function"]["description"] += (
                " Configured app aliases: " + ", ".join(sorted(self._applications)) + "."
            )
        else:
            app_schema["description"] = (
                "No applications are configured. Ask the operator to add an alias "
                "to AKARYON_DESKTOP_APPS_JSON; do not attempt another launch method."
            )
        self._launches: dict[str, tuple[str, subprocess.Popen]] = {}
        self._closing: set[str] = set()
        self._launch_lock = threading.Lock()

    @property
    def application_aliases(self) -> list[str]:
        return sorted(self._applications)

    def validate(self, operation: str, application: str | None = None,
                 launch_id: str | None = None) -> str | None:
        if sys.platform != "win32":
            return "Windows application control is available only on Windows"
        if operation == "open":
            if not isinstance(application, str) or application not in self._applications:
                return "Choose an application configured in AKARYON_DESKTOP_APPS_JSON"
            if launch_id is not None:
                return "launch_id is only valid for close"
            if not Path(self._applications[application]).is_file():
                return "The configured Windows app executable does not exist"
            return None
        if operation == "close":
            if application is not None:
                return "application is only valid for open"
            if not isinstance(launch_id, str):
                return "A launch_id returned by windows_app open is required"
            with self._launch_lock:
                if launch_id in self._closing:
                    return "A graceful close request was already sent for this launch"
                launch = self._launches.get(launch_id)
            if launch is None:
                return "This app was not opened by Akaryon in the current session"
            _, process = launch
            if process.poll() is not None:
                with self._launch_lock:
                    self._launches.pop(launch_id, None)
                return "The app process has already exited"
            return None
        return "operation must be open or close"

    @staticmethod
    def _child_environment() -> dict[str, str]:
        return {name: value for name, value in os.environ.items()
                if not _SECRET_ENV.search(name)}

    def execute(self, operation: str, application: str | None = None,
                launch_id: str | None = None) -> ToolResult:
        issue = self.validate(operation, application, launch_id)
        if issue:
            return ToolResult(False, error=issue)
        if operation == "open":
            return self._open(application)
        return self._close(launch_id)

    def _open(self, application: str) -> ToolResult:
        executable = self._applications[application]
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            process = subprocess.Popen(
                [executable], cwd=str(Path(executable).parent), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                env=self._child_environment(), creationflags=flags,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return ToolResult(False, error="Could not start the configured Windows app")
        if process.poll() is not None:
            return ToolResult(False, error="The configured app exited before it could be tracked")
        launch_id = str(uuid.uuid4())
        with self._launch_lock:
            self._launches[launch_id] = (application, process)
            for stale_id, (_, stale_process) in list(self._launches.items()):
                if stale_process.poll() is not None:
                    self._launches.pop(stale_id, None)
                    self._closing.discard(stale_id)
        return ToolResult(True, output={
            "opened": True,
            "application": application,
            "launch_id": launch_id,
            "message": "App launched. Use this launch_id to request a graceful close later.",
        })

    def _close(self, launch_id: str) -> ToolResult:
        with self._launch_lock:
            launch = self._launches.get(launch_id)
            if launch is None:
                return ToolResult(False, error="This app was not opened by Akaryon in the current session")
            if launch_id in self._closing:
                return ToolResult(False, error="A graceful close request was already sent for this launch")
            self._closing.add(launch_id)
        application, process = launch
        window_count = self._request_window_close(process.pid)
        if window_count == 0:
            with self._launch_lock:
                self._closing.discard(launch_id)
            return ToolResult(False, error="No visible window was found for this Akaryon-launched app")
        return ToolResult(True, output={
            "close_requested": True,
            "application": application,
            "windows_notified": window_count,
            "message": "A graceful close request was sent; the app may show an unsaved-changes prompt.",
        })

    @staticmethod
    def _request_window_close(process_id: int) -> int:
        if sys.platform != "win32":
            return 0
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_windows = user32.EnumWindows
        enum_windows.argtypes = (ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p),
                                 ctypes.c_void_p)
        enum_windows.restype = ctypes.c_bool
        get_window_pid = user32.GetWindowThreadProcessId
        get_window_pid.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        get_window_pid.restype = ctypes.c_ulong
        is_visible = user32.IsWindowVisible
        is_visible.argtypes = (ctypes.c_void_p,)
        is_visible.restype = ctypes.c_bool
        post_message = user32.PostMessageW
        post_message.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
        post_message.restype = ctypes.c_bool
        close_message = 0x0010  # WM_CLOSE
        windows: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def collect(hwnd, _parameter):
            if not is_visible(hwnd):
                return True
            owner = ctypes.c_ulong()
            get_window_pid(hwnd, ctypes.byref(owner))
            if owner.value == process_id:
                windows.append(int(hwnd))
            return True

        if not enum_windows(collect, None):
            return 0
        return sum(bool(post_message(hwnd, close_message, 0, 0)) for hwnd in windows)
