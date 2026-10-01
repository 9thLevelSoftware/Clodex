"""Tests: audit quorum, reviewer resilience and fix collection."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from clodex.prompts import audit_diff_excerpt
from clodex.quorum import evaluate, parse_quorum, quorum_met, required_fixes, verdict_approved
from clodex.workflow import ClodexWorkflow
from tests.support import FakeCliPath, TempRepo

H = "a" * 64


def verdict(reviewer_id: str, approved: bool = True, required: bool = True, **extra) -> dict:
    return {"reviewer_id": reviewer_id, "approved": approved, "diff_hash": H, "persona": "p", "_required": required, **extra}


class QuorumTests(unittest.TestCase):
    def test_parse_quorum(self):
        self.assertEqual(parse_quorum("unanimous"), ("unanimous", 0))
        self.assertEqual(parse_quorum(" ALL "), ("unanimous", 0))
        self.assertEqual(parse_quorum("majority"), ("majority", 0))
        self.assertEqual(parse_quorum(2), ("count", 2))
        self.assertEqual(parse_quorum("3"), ("count", 3))
        for bad in ("most", True, 0, "-1", ""):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_quorum(bad)

    def test_quorum_met(self):
        self.assertTrue(quorum_met(2, 2, "unanimous"))
        self.assertFalse(quorum_met(1, 2, "unanimous"))
        self.assertFalse(quorum_met(0, 0, "unanimous"), "no required reviewers must never pass")
        self.assertFalse(quorum_met(0, 0, "majority"))
        self.assertTrue(quorum_met(2, 3, "majority"))
        self.assertFalse(quorum_met(1, 2, "majority"))
        self.assertTrue(quorum_met(3, 4, "majority"))
        self.assertTrue(quorum_met(2, 3, 2))
        self.assertFalse(quorum_met(1, 3, 2))
        self.assertTrue(quorum_met(2, 2, 5), "N larger than the reviewer count is clamped")

    def test_evaluate_counts_required_reviewers_only_and_exact_diff_hash(self):
        verdicts = [
            verdict("a"),
            verdict("b", approved=False),
            verdict("opt", approved=False, required=False),
            verdict("stale", required=False, diff_hash="other"),
        ]
        result = evaluate(verdicts, H, attempt=1, quorum="unanimous")
        self.assertFalse(result["approved"])
        self.assertEqual(result["required_approved_by"], ["a"])
        self.assertEqual(result["required_pending"], ["b"])
        self.assertEqual(result["attempt"], 1)
        self.assertFalse(result["reviewers"]["stale"]["approved"])
        self.assertNotIn("claude_approved", result)
        self.assertTrue(evaluate([verdict("a"), verdict("opt", approved=False, required=False)], H)["approved"])
        self.assertTrue(evaluate(verdicts[:2], H, quorum="majority")["approved"] is False)

    def test_errored_reviewer_never_approves(self):
        broken = verdict("a", _error="exit code 124")
        self.assertFalse(verdict_approved(broken, H))
        result = evaluate([broken], H)
        self.assertFalse(result["approved"])
        self.assertEqual(result["reviewers"]["a"]["error"], "exit code 124")

    def test_required_fixes_only_from_blocking_reviewers_deduplicated_and_ordered(self):
        verdicts = [
            verdict("a", approved=False, required_fixes=["add tests", "add tests"], findings=[
                {"severity": "low", "file": None, "line": None, "message": "nit"},
                {"severity": "critical", "file": "x.py", "line": 3, "message": "crash"},
            ]),
            verdict("b", approved=False, required_fixes=["add tests", "update docs"]),
            verdict("ok", approved=True, required_fixes=["ignored: approved"]),
            verdict("opt", approved=False, required=False, required_fixes=["ignored: optional"]),
            verdict("broken", approved=False, _error="boom", required_fixes=["ignored: errored"]),
        ]
        self.assertEqual(
            required_fixes(verdicts, H),
            ["add tests", "update docs", "[critical] x.py:3: crash", "[low] nit"],
        )
        self.assertEqual(len(required_fixes([verdict("ok")], H)), 1, "falls back to a generic instruction")


class DiffExcerptTests(unittest.TestCase):
    def test_small_diff_is_unchanged(self):
        self.assertEqual(audit_diff_excerpt("diff --git a/x b/x\n+1\n", 1000), "diff --git a/x b/x\n+1\n")
        self.assertEqual(audit_diff_excerpt("anything", 0), "anything")

    def test_large_diff_is_truncated_with_file_list(self):
        diff = "diff --git a/one.py b/one.py\n" + "+" + "é" * 500 + "\ndiff --git a/two.py b/two.py\n+x\n"
        shown = audit_diff_excerpt(diff, 100)
        self.assertLess(len(shown.encode("utf-8")), len(diff.encode("utf-8")))
        self.assertIn("diff truncated", shown)
        self.assertIn("one.py, two.py", shown)


def contract(repo: Path, reviewers: list[dict], quorum: str = "unanimous") -> None:
    (repo / "CLODEX.md").write_text(
        f"---\nmax_fix_loops: 0\naudit:\n  quorum: {quorum}\n  reviewers: {json.dumps(reviewers)}\n---\nbody\n",
        encoding="utf-8",
    )


def reviewer(reviewer_id: str, backend: str, required: bool, timeout: int = 600) -> dict:
    return {"id": reviewer_id, "backend": backend, "persona": reviewer_id, "required": required, "timeout": timeout}


REQUIRED_PAIR = [reviewer("claude-plan", "claude", True), reviewer("codex-architecture", "codex", True)]


class AuditWorkflowTests(unittest.TestCase):
    def test_failed_optional_reviewer_does_not_abort_the_audit(self):
        with TempRepo() as repo, FakeCliPath(fail_reviewers=("security",)):
            contract(repo, [*REQUIRED_PAIR, reviewer("security", "codex", False)])
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "approved")
            security = result.data["reviewers"]["security"]
            self.assertFalse(security["approved"])
            self.assertIn("exit code 3", security["error"])
            saved = json.loads((Path(result.artifacts_dir) / "reviewers" / "security.json").read_text(encoding="utf-8"))
            self.assertIn("_error", saved)

    def test_timed_out_optional_reviewer_does_not_abort_the_audit(self):
        with TempRepo() as repo, FakeCliPath(slow_reviewers=("security",)):
            contract(repo, [*REQUIRED_PAIR, reviewer("security", "codex", False, timeout=1)])
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "approved")
            self.assertIn("exit code 124", result.data["reviewers"]["security"]["error"])

    def test_failed_required_reviewer_blocks_without_a_pointless_fix_attempt(self):
        with TempRepo() as repo, FakeCliPath(fail_reviewers=("codex-architecture",)):
            contract(repo, REQUIRED_PAIR)
            workflow = ClodexWorkflow(repo)
            result = workflow.build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "blocked")
            self.assertIn("codex-architecture", result.data["error"])
            self.assertFalse(list(Path(result.artifacts_dir).glob("fix-attempt-*")))
            run = workflow.state.get_run(result.run_id)
            self.assertEqual(run["status"], "blocked")
            self.assertIn("codex-architecture", run["error"])

    def test_majority_quorum_approves_despite_one_required_rejection(self):
        reviewers = [*REQUIRED_PAIR, reviewer("security", "codex", True)]
        with TempRepo() as repo, FakeCliPath(reject_reviewers=("security",)):
            contract(repo, reviewers, quorum="majority")
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "approved")
            self.assertEqual(result.data["required_pending"], ["security"])
            self.assertEqual(sorted(result.data["required_approved_by"]), ["claude-plan", "codex-architecture"])
        with TempRepo() as repo, FakeCliPath(reject_reviewers=("security",)):
            contract(repo, reviewers, quorum="unanimous")
            self.assertEqual(ClodexWorkflow(repo).build("implement fixture", workspace_backend="local").status, "blocked")

    def test_agreement_has_no_hardcoded_reviewer_fields(self):
        with TempRepo() as repo, FakeCliPath():
            contract(repo, [reviewer("alpha", "claude", True), reviewer("beta", "codex", True)])
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "approved")
            self.assertNotIn("claude_approved", result.data)
            self.assertEqual(sorted(result.data["required_approved_by"]), ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
