from __future__ import annotations

import os
import subprocess
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .commands import AgentCommand


@dataclass
class AgentResult:
    command: AgentCommand
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    # Final agent message read from `-o <file>` when the command asked for it.
    last_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """The text to parse: the captured final message if any, else stdout."""
        return self.last_message if self.last_message else self.stdout


class AgentRunner:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    def run(self, command: AgentCommand, prompt: str, timeout: int | None = None) -> AgentResult:
        argv = list(command.argv)
        resolved = shutil.which(argv[0])
        if resolved:
            argv[0] = resolved
        last_message_path: str | None = None
        if command.capture_last_message:
            handle, last_message_path = tempfile.mkstemp(prefix="clodex-last-message-", suffix=".txt")
            os.close(handle)
            # Keep the trailing "-" (read prompt from stdin) last.
            at = len(argv) - 1 if argv and argv[-1] == "-" else len(argv)
            argv[at:at] = ["-o", last_message_path]
        try:
            try:
                result = subprocess.run(
                    argv,
                    cwd=self.repo_root,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                return AgentResult(
                    command=command,
                    stdout=_as_text(exc.stdout),
                    stderr=_as_text(exc.stderr) + f"\ntimed out after {timeout}s",
                    returncode=124,
                    timed_out=True,
                )
            return AgentResult(
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
                last_message=_read_text(last_message_path),
            )
        finally:
            if last_message_path:
                try:
                    os.unlink(last_message_path)
                except OSError:
                    pass


def _read_text(path: str | None) -> str | None:
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text.strip() or None


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
