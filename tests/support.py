"""Shared helpers for the Clodex test suite: temp git repos and strict fake claude/codex CLIs."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def cli_test_python() -> str | None:
    """Python used to launch Clodex in subprocess tests (override with CLODEX_TEST_PYTHON)."""
    override = os.environ.get("CLODEX_TEST_PYTHON")
    if override:
        return override
    if sys.version_info >= (3, 12):
        return sys.executable
    return None


def cleanup_tempdir(tmp: tempfile.TemporaryDirectory, attempts: int = 50) -> None:
    # Orphaned fake CLI children can briefly hold the directory on Windows.
    for attempt in range(attempts):
        try:
            tmp.cleanup()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)


class TempRepo:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        shutil.copy(ROOT / "CLODEX.md", self.path / "CLODEX.md")
        subprocess.run(["git", "init"], cwd=self.path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.path, check=True)
        (self.path / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "CLODEX.md", "seed.txt"], cwd=self.path, check=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=self.path, check=True, capture_output=True)
        return self.path

    def __exit__(self, exc_type, exc, tb):
        cleanup_tempdir(self.tmp)


class FakeCliPath:
    def __init__(
        self,
        reject_once: bool = False,
        malformed_once: bool = False,
        sleep_seconds: float = 0,
        include_clodex: bool = False,
        envelope_error_once: bool = False,
    ):
        self.reject_once = reject_once
        self.malformed_once = malformed_once
        self.envelope_error_once = envelope_error_once
        self.sleep_seconds = sleep_seconds
        self.include_clodex = include_clodex

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        self._write_fake()
        self.old_path = os.environ.get("PATH", "")
        self.old_pathext = os.environ.get("PATHEXT", "")
        os.environ["PATH"] = str(self.bin) + os.pathsep + self.old_path
        if os.name == "nt":
            os.environ["PATHEXT"] = ".CMD;.BAT;.EXE;" + self.old_pathext
        return self.bin

    def __exit__(self, exc_type, exc, tb):
        os.environ["PATH"] = self.old_path
        if os.name == "nt":
            os.environ["PATHEXT"] = self.old_pathext
        cleanup_tempdir(self.tmp)

    def _write_fake(self):
        fake = self.bin / "fake_cli.py"
        fake.write_text(
            f"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

name = sys.argv[1]
args = sys.argv[2:]
if '--version' in args:
    # Answer before reading stdin: callers like `doctor` don't close it.
    print(name + ' fake 1.0.0')
    raise SystemExit(0)
stdin = sys.stdin.read()

if {self.sleep_seconds!r}:
    import time
    time.sleep({self.sleep_seconds!r})

def diff_hash():
    out = subprocess.run(['git', 'diff', '--binary', 'HEAD'], capture_output=True, text=True, encoding='utf-8').stdout
    return hashlib.sha256(out.encode('utf-8')).hexdigest()

def requested_hash():
    match = re.search(r'Diff hash: ([a-f0-9]{{64}})', stdin)
    return match.group(1) if match else diff_hash()

def fail(message):
    sys.stderr.write(message + '\\n')
    raise SystemExit(2)

def parse_flags(value_flags, bool_flags):
    # Strict like the real CLIs: unknown flags and positional args exit 2.
    parsed = {{}}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '-':
            i += 1
        elif arg in bool_flags:
            parsed[arg] = True
            i += 1
        elif arg in value_flags:
            if i + 1 >= len(args):
                fail("error: a value is required for '" + arg + "'")
            parsed.setdefault(arg, []).append(args[i + 1])
            i += 2
        else:
            fail("error: unexpected argument '" + arg + "' found")
    return parsed

def audit_verdict(approved, summary, fixes):
    h = requested_hash()
    reviewer = 'claude-plan' if name == 'claude' else 'codex-architecture'
    persona = 'plan-adherence' if name == 'claude' else 'architecture'
    reviewer_match = re.search(r'Reviewer ID: ([^\\n]+)', stdin)
    persona_match = re.search(r'Persona: ([^\\n]+)', stdin)
    if reviewer_match:
        reviewer = reviewer_match.group(1).strip()
    if persona_match:
        persona = persona_match.group(1).strip()
    return json.dumps({{'approved': approved, 'diff_hash': h, 'reviewer_id': reviewer, 'persona': persona, 'summary': summary, 'findings': [], 'required_fixes': fixes}})

if name == 'claude':
    flags = parse_flags(
        {{'--model', '--effort', '--permission-mode', '--output-format', '--json-schema', '--fallback-model', '--max-budget-usd', '--append-system-prompt'}},
        {{'-p', '--print', '--bare'}},
    )
    if '-p' not in flags and '--print' not in flags:
        fail('error: fake claude only supports --print mode')
    if flags.get('--effort', ['high'])[-1] not in ('low', 'medium', 'high', 'xhigh', 'max'):
        fail('error: invalid --effort')
    if flags.get('--permission-mode', ['plan'])[-1] not in ('acceptEdits', 'auto', 'bypassPermissions', 'manual', 'dontAsk', 'plan'):
        fail('error: invalid --permission-mode')
    as_json = flags.get('--output-format', ['text'])[-1] == 'json'

    def emit(text, is_error=False):
        if as_json:
            # The real `claude -p --output-format json` wraps the answer in an envelope.
            print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': is_error, 'result': text, 'total_cost_usd': 0, 'session_id': 'fake'}}))
        else:
            print(text)
        raise SystemExit(0)

    error_marker = Path('.fake-claude-envelope-error')
    if {str(self.envelope_error_once)!r} == 'True' and not error_marker.exists():
        error_marker.write_text('seen')
        emit('Not logged in', is_error=True)
    marker = Path('.fake-claude-malformed')
    if {str(self.malformed_once)!r} == 'True' and not marker.exists():
        marker.write_text('seen')
        emit('not json')
    if 'adversarial auditor' in stdin:
        reject_marker = Path('.fake-claude-reject')
        if {str(self.reject_once)!r} == 'True' and not reject_marker.exists():
            reject_marker.write_text('seen')
            emit(audit_verdict(False, 'reject once', ['append fixed line']))
        emit(audit_verdict(True, 'ok', []))
    emit(json.dumps({{'goal': 'test goal', 'scope': ['repo'], 'out_of_scope': [], 'implementation_spec': ['write implemented.txt'], 'acceptance_criteria': ['diff exists'], 'risks': [], 'test_commands': ['python -m unittest']}}))

if name == 'codex':
    if not args or args[0] != 'exec':
        fail("error: fake codex only supports the `exec` subcommand")
    args = args[1:]
    flags = parse_flags(
        {{'-m', '-c', '-s', '--sandbox', '-C', '--output-schema', '-o', '--output-last-message'}},
        {{'--approve-for-me', '--json', '--ephemeral', '--skip-git-repo-check', '--strict-config'}},
    )
    for item in flags.get('-c', []):
        key, _, value = item.partition('=')
        value = value.strip('"')
        if key == 'model_reasoning_effort':
            if value not in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
                fail('error: invalid model_reasoning_effort ' + value)
        elif key == 'approval_policy':
            if value not in ('untrusted', 'on-failure', 'on-request', 'never'):
                fail('error: invalid approval_policy ' + value)
        elif key != 'model':
            fail('error: unknown config key ' + key)
    sandbox = (flags.get('-s') or flags.get('--sandbox') or ['read-only'])[-1]
    if sandbox not in ('read-only', 'workspace-write', 'danger-full-access'):
        fail('error: invalid sandbox ' + sandbox)
    if '-C' in flags:
        os.chdir(flags['-C'][-1])

    def finish(text):
        out = flags.get('-o') or flags.get('--output-last-message')
        if out:
            Path(out[-1]).write_text(text, encoding='utf-8')
        print(text)
        raise SystemExit(0)

    if 'adversarial auditor' in stdin:
        finish(audit_verdict(True, 'ok', []))
    if 'Required fixes' in stdin:
        Path('implemented.txt').write_text('implemented\\nfixed\\n', encoding='utf-8')
        finish('fixed implementation')
    Path('implemented.txt').write_text('implemented\\n', encoding='utf-8')
    finish('implemented')

raise SystemExit(2)
""",
            encoding="utf-8",
        )
        names = ["claude", "codex"]
        if self.include_clodex:
            names.append("clodex")
        if os.name == "nt":
            for name in names:
                (self.bin / f"{name}.cmd").write_text(
                    f"@echo off\r\n\"{sys.executable}\" \"%~dp0fake_cli.py\" {name} %*\r\n",
                    encoding="utf-8",
                )
        else:
            for name in names:
                path = self.bin / name
                path.write_text(f"#!/usr/bin/env bash\nexec \"{sys.executable}\" \"$(dirname \"$0\")/fake_cli.py\" {name} \"$@\"\n", encoding="utf-8")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
