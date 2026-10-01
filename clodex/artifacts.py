from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ClodexConfig


def slugify(value: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return (slug or "task")[:max_len].strip("-") or "task"


def make_task_id(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    return f"{slugify(prompt, 36)}-{digest}"


def make_run_id(task_id: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{task_id[:32]}-{uuid.uuid4().hex[:8]}"


class ArtifactStore:
    def __init__(self, config: ClodexConfig, run_id: str, state: Any = None):
        self.config = config
        self.run_id = run_id
        self.state = state
        self.path = config.runs_root / run_id
        self.path.mkdir(parents=True, exist_ok=True)

    def _record(self, name: str, path: Path) -> None:
        """Index the file in the state ledger so `handoff_get` and exports can find it."""
        if self.state is not None:
            self.state.add_artifact(self.run_id, name, str(path), path.suffix.lstrip(".") or "file")

    def write_json(self, name: str, data: dict[str, Any]) -> Path:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._record(name, path)
        return path

    def write_text(self, name: str, text: str, exact: bool = False) -> Path:
        """Write text; `exact=True` keeps bytes as-is (no newline translation), for diffs and patches."""
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if exact:
            path.write_bytes(text.encode("utf-8", errors="surrogateescape"))
        else:
            path.write_text(text, encoding="utf-8")
        self._record(name, path)
        return path


def current_diff(repo_root: Path) -> str:
    # Read bytes, not text: universal-newline decoding would rewrite CRLF content and
    # the diff would no longer apply to (or hash like) the real files.
    result = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=repo_root, capture_output=True, check=False)
    return result.stdout.decode("utf-8", errors="surrogateescape")


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()
