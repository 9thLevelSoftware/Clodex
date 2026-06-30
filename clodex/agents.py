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

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class AgentRunner:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    def run(self, command: AgentCommand, prompt: str) -> AgentResult:
        argv = list(command.argv)
        resolved = shutil.which(argv[0])
        if resolved:
            argv[0] = resolved
        result = subprocess.run(
            argv,
            cwd=self.repo_root,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return AgentResult(
            command=command,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )
