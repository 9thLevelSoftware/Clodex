from __future__ import annotations

import subprocess
import shutil
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

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class AgentRunner:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    def run(self, command: AgentCommand, prompt: str, timeout: int | None = None) -> AgentResult:
        argv = list(command.argv)
        resolved = shutil.which(argv[0])
        if resolved:
            argv[0] = resolved
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
        )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
