from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ClodexConfig


class DirtyWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceRef:
    backend: str
    source_path: Path
    path: Path
    is_worktree: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "source_path": str(self.source_path),
            "path": str(self.path),
            "is_worktree": self.is_worktree,
        }


class WorkspaceManager:
    def __init__(self, repo_root: Path, config: ClodexConfig):
        self.repo_root = repo_root.resolve()
        self.config = config

    def prepare(self, run_id: str, backend: str | None = None) -> WorkspaceRef:
        selected = backend or str(self.config.workspace.get("backend", "git-worktree"))
        if selected == "local":
            return WorkspaceRef("local", self.repo_root, self.repo_root, False)
        if selected != "git-worktree":
            raise ValueError(f"Unsupported workspace backend: {selected}")
        self._ensure_clean_tracked()
        path = (self.config.workspace_root / run_id).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            subprocess.run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=self.repo_root, check=True, capture_output=True, text=True)
        return WorkspaceRef("git-worktree", self.repo_root, path, True)

    def write_metadata(self, run_dir: Path, workspace: WorkspaceRef) -> Path:
        path = run_dir / "workspace.json"
        path.write_text(json.dumps(workspace.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _ensure_clean_tracked(self) -> None:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise DirtyWorkspaceError(result.stderr.strip() or "Could not inspect git status")
        if result.stdout.strip():
            raise DirtyWorkspaceError("Tracked changes are present; use --workspace local or commit/stash before git-worktree builds.")
