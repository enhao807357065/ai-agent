"""Filesystem and process boundary for guarded repository tools."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Mapping


class SandboxViolation(RuntimeError):
    pass


class Sandbox:
    def __init__(self, *, workspace: str | Path, command_allowlist: set[str], timeout_seconds: float = 30, output_limit: int = 20_000):
        self.workspace = Path(workspace).resolve()
        self.command_allowlist = command_allowlist
        self.command_aliases = {"uv": "python"}
        self.timeout_seconds = timeout_seconds
        self.output_limit = output_limit

    def _path(self, value: str) -> Path:
        candidate = (self.workspace / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise SandboxViolation("PATH_OUTSIDE_WORKSPACE")
        protected = {".git", ".env", ".ssh", "secrets"}
        if any(part in protected for part in candidate.relative_to(self.workspace).parts):
            raise SandboxViolation("PATH_SENSITIVE_DIRECTORY")
        return candidate

    def read_text(self, path: str) -> str:
        return self._path(path).read_text()

    def list_files(self, path: str = ".") -> list[str]:
        root = self._path(path)
        if not root.is_dir():
            raise SandboxViolation("PATH_NOT_DIRECTORY")
        protected = {".git", ".env", ".ssh", "secrets"}
        return sorted(
            str(item.relative_to(self.workspace))
            for item in root.rglob("*")
            if item.is_file() and not any(part in protected for part in item.relative_to(self.workspace).parts)
        )

    def search_text(self, query: str, path: str = ".", max_results: int = 100) -> list[dict[str, object]]:
        if not query:
            raise SandboxViolation("SEARCH_QUERY_EMPTY")
        matches: list[dict[str, object]] = []
        for relative_path in self.list_files(path):
            try:
                lines = self.read_text(relative_path).splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append({"path": relative_path, "line": line_number, "text": line[:500]})
                    if len(matches) >= max_results:
                        return matches
        return matches

    def write_text(self, path: str, content: str) -> None:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def safe_environment(self, source: Mapping[str, str] | None = None) -> dict[str, str]:
        source = os.environ if source is None else source
        return {key: value for key, value in source.items() if not any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"))}

    @staticmethod
    def _failure_kind(*, exit_code: int, stdout: str, stderr: str) -> str | None:
        if exit_code == 0:
            return None
        details = f"{stdout}\n{stderr}".lower()
        invalid_markers = (
            "could not find",
            "no tests ran",
            "not found:",
            "err_module_not_found",
            "cannot find package",
            "enoent",
            "no module named",
            "command not found",
        )
        if any(marker in details for marker in invalid_markers):
            return "TEST_COMMAND_INVALID"
        return "TEST_FAILED"

    def run_command(self, argv: list[str], *, cwd: str = ".") -> dict[str, object]:
        command_cwd = self._path(cwd)
        if not command_cwd.is_dir():
            raise SandboxViolation("CWD_NOT_DIRECTORY")
        allowed = bool(argv) and argv[0] in self.command_allowlist
        # uv is a repository's Python launcher, not an unrestricted shell. Permit
        # only ``uv run <allowlisted-command> ...``.
        if argv and argv[0] == "uv" and len(argv) >= 3 and argv[1] == "run":
            allowed = argv[2] in self.command_allowlist
        if not allowed:
            raise SandboxViolation("COMMAND_NOT_ALLOWED")
        process = subprocess.Popen(
            argv,
            cwd=command_cwd,
            env=self.safe_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            # start_new_session makes PID the process-group leader. Kill the full
            # group so a shell/runner child cannot continue after timeout.
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            raise SandboxViolation("COMMAND_TIMED_OUT") from exc
        return {
            "exit_code": process.returncode,
            "failure_kind": self._failure_kind(exit_code=process.returncode, stdout=stdout, stderr=stderr),
            "stdout": stdout[: self.output_limit],
            "stderr": stderr[: self.output_limit],
            "truncated": len(stdout) > self.output_limit or len(stderr) > self.output_limit,
        }
