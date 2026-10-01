"""Tests: managed blocks and native install rendering."""

from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path
from clodex.native import (
    BEGIN_MARKER,
    END_MARKER,
    ManagedBlockError,
    TOML_BEGIN_MARKER,
    TOML_END_MARKER,
    build_agents_block,
    build_claude_block,
    replace_managed_block,
    replace_toml_managed_block,
)
from tests.support import TempRepo


class NativeTests(unittest.TestCase):
    def test_replace_managed_block_preserves_unmanaged_content(self):
        existing = "header\n<!-- BEGIN CLODEX -->\nold\n<!-- END CLODEX -->\nfooter\n"
        updated, changed = replace_managed_block(existing, "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n<!-- BEGIN CLODEX -->\nnew\n<!-- END CLODEX -->\nfooter\n")

    def test_replace_managed_block_appends_when_missing(self):
        updated, changed = replace_managed_block("header\n", "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n\n<!-- BEGIN CLODEX -->\nnew\n<!-- END CLODEX -->\n")

    def test_replace_managed_block_is_idempotent(self):
        existing = f"{BEGIN_MARKER}\nnew\n{END_MARKER}\n"
        updated, changed = replace_managed_block(existing, "new\n")
        self.assertFalse(changed)
        self.assertEqual(updated, existing)

    def test_replace_managed_block_rejects_malformed_block_without_force(self):
        with self.assertRaises(ManagedBlockError):
            replace_managed_block("header\n<!-- BEGIN CLODEX -->\nmissing end\n", "new\n")

    def test_replace_managed_block_force_replaces_malformed_tail(self):
        updated, changed = replace_managed_block("header\n<!-- BEGIN CLODEX -->\nmissing end\n", "new\n", force=True)
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n<!-- BEGIN CLODEX -->\nnew\n<!-- END CLODEX -->\n")

    def test_replace_managed_block_preserves_crlf_style(self):
        existing = "header\r\n<!-- BEGIN CLODEX -->\r\nold\r\n<!-- END CLODEX -->\r\nfooter\r\n"
        updated, changed = replace_managed_block(existing, "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\r\n<!-- BEGIN CLODEX -->\r\nnew\r\n<!-- END CLODEX -->\r\nfooter\r\n")

    def test_replace_toml_managed_block_appends_when_missing(self):
        updated, changed = replace_toml_managed_block("header\n", "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n\n# BEGIN CLODEX\nnew\n# END CLODEX\n")

    def test_replace_toml_managed_block_is_idempotent(self):
        existing = f"{TOML_BEGIN_MARKER}\nnew\n{TOML_END_MARKER}\n"
        updated, changed = replace_toml_managed_block(existing, "new\n")
        self.assertFalse(changed)
        self.assertEqual(updated, existing)

    def test_replace_toml_managed_block_rejects_malformed_block_without_force(self):
        with self.assertRaises(ManagedBlockError):
            replace_toml_managed_block("header\n# BEGIN CLODEX\nmissing end\n", "new\n")

    def test_replace_toml_managed_block_force_replaces_malformed_tail(self):
        updated, changed = replace_toml_managed_block("header\n# BEGIN CLODEX\nmissing end\n", "new\n", force=True)
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n# BEGIN CLODEX\nnew\n# END CLODEX\n")

    def test_replace_toml_managed_block_preserves_crlf_style(self):
        existing = "header\r\n# BEGIN CLODEX\r\nold\r\n# END CLODEX\r\nfooter\r\n"
        updated, changed = replace_toml_managed_block(existing, "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\r\n# BEGIN CLODEX\r\nnew\r\n# END CLODEX\r\nfooter\r\n")

    def test_native_instruction_templates_include_mcp_and_cli_fallbacks(self):
        claude = build_claude_block()
        agents = build_agents_block()
        self.assertIn("clodex_handoff_create", claude)
        self.assertIn("clodex_handoff_decide", claude)
        self.assertIn("clodex task start", claude)
        self.assertIn("Codex is the default engineer", claude)
        self.assertIn("clodex_handoff_update", agents)
        self.assertIn("Claude Code is the default strategist", agents)
        self.assertIn("handoff budget", agents)

    def test_native_plan_dry_run_previews_instruction_and_mcp_files(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            plan = plan_native_install(repo, dry_run=True)
        paths = {Path(item["path"]).name for item in plan["files"]}
        self.assertIn("CLAUDE.md", paths)
        self.assertIn("AGENTS.md", paths)
        self.assertIn("CLODEX.md", paths)
        self.assertIn(".mcp.json", paths)
        self.assertIn("config.toml", paths)
        self.assertTrue(plan["dry_run"])
        self.assertTrue(any("clodex_handoff_create" in item["preview"] for item in plan["files"]))

    def test_native_plan_no_mcp_config_skips_mcp_files(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            plan = plan_native_install(repo, dry_run=True, no_mcp_config=True)
        paths = {str(Path(item["path"]).as_posix()) for item in plan["files"]}
        self.assertFalse(any(path.endswith(".mcp.json") for path in paths))
        self.assertFalse(any(path.endswith(".codex/config.toml") for path in paths))

    def test_native_plan_reports_invalid_utf8_target(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            (repo / "CLAUDE.md").write_bytes(b"\xff\xfe\xff")
            plan = plan_native_install(repo, no_mcp_config=True)
        item = next(item for item in plan["files"] if Path(item["path"]).name == "CLAUDE.md")
        self.assertEqual(item["status"], "invalid")
        self.assertEqual(item["action"], "error")
        self.assertIn("utf-8", item["error"].lower())

    def test_native_plan_reports_directory_target_as_invalid(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            (repo / "CLAUDE.md").mkdir()
            plan = plan_native_install(repo, no_mcp_config=True)
        item = next(item for item in plan["files"] if Path(item["path"]).name == "CLAUDE.md")
        self.assertEqual(item["status"], "invalid")
        self.assertEqual(item["action"], "error")
        self.assertEqual(item["preview"], "")
        self.assertIn("CLAUDE.md", item["error"])

    def test_native_plan_reports_invalid_unrelated_toml_config(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            config = repo / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text(
                "[mcp_servers.other]\n"
                'command = "one"\n'
                "\n"
                "[mcp_servers.other]\n"
                'command = "two"\n',
                encoding="utf-8",
            )
            plan = plan_native_install(repo)
        item = next(item for item in plan["files"] if item["path"].endswith(".codex\\config.toml") or item["path"].endswith(".codex/config.toml"))
        self.assertEqual(item["status"], "invalid")
        self.assertEqual(item["action"], "error")
        self.assertIn("Invalid .codex/config.toml", item["error"])

    def test_apply_native_install_rejects_invalid_target_without_partial_writes(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            (repo / "CLAUDE.md").write_bytes(b"\xff\xfe\xff")
            clodex_before = (repo / "CLODEX.md").read_bytes()
            with self.assertRaisesRegex(ManagedBlockError, "CLAUDE.md"):
                apply_native_install(repo, no_mcp_config=True)
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertEqual((repo / "CLAUDE.md").read_bytes(), b"\xff\xfe\xff")
            self.assertEqual((repo / "CLODEX.md").read_bytes(), clodex_before)

    def test_apply_native_install_rejects_directory_target_without_partial_writes(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            (repo / "CLAUDE.md").mkdir()
            clodex_before = (repo / "CLODEX.md").read_bytes()
            with self.assertRaisesRegex(ManagedBlockError, "CLAUDE.md"):
                apply_native_install(repo, no_mcp_config=True)
            self.assertTrue((repo / "CLAUDE.md").is_dir())
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertEqual((repo / "CLODEX.md").read_bytes(), clodex_before)

    def test_apply_native_install_rejects_parent_file_conflict_without_partial_writes(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            clodex_before = (repo / "CLODEX.md").read_bytes()
            (repo / ".codex").write_text("not a directory\n", encoding="utf-8")
            with self.assertRaisesRegex(ManagedBlockError, ".codex"):
                apply_native_install(repo)
            self.assertFalse((repo / "CLAUDE.md").exists())
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertFalse((repo / ".mcp.json").exists())
            self.assertTrue((repo / ".codex").is_file())
            self.assertEqual((repo / "CLODEX.md").read_bytes(), clodex_before)

    def test_apply_native_install_preserves_crlf_codex_config(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            config = repo / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_bytes(b'model = "gpt-5.5"\r\n')
            apply_native_install(repo)
            content = config.read_bytes()
        self.assertIn(b"# BEGIN CLODEX\r\n", content)
        self.assertIn(b"[mcp_servers.clodex]\r\n", content)
        self.assertIn(b'args = ["mcp-server"]\r\n', content)

    def test_apply_native_install_force_adopts_crlf_unmanaged_codex_config(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            config = repo / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_bytes(
                b'model = "gpt-5.5"\r\n'
                b"\r\n"
                b"[mcp_servers.clodex]\r\n"
                b'command = "old-clodex"\r\n'
                b'args = ["old"]\r\n'
                b"\r\n"
                b"[mcp_servers.other]\r\n"
                b'command = "other"\r\n'
            )
            apply_native_install(repo, force=True)
            content = config.read_bytes()
        self.assertEqual(content.count(b"[mcp_servers.clodex]"), 1)
        self.assertIn(b"# BEGIN CLODEX\r\n", content)
        self.assertIn(b'command = "clodex"\r\n', content)
        self.assertIn(b"[mcp_servers.other]\r\n", content)
        self.assertIn(b'command = "other"\r\n', content)

    def test_render_mcp_json_preserves_existing_servers(self):
        from clodex.native import render_mcp_json

        rendered = render_mcp_json('{"mcpServers":{"existing":{"command":"node","args":["server.js"]}}}')
        data = json.loads(rendered)
        self.assertEqual(data["mcpServers"]["existing"]["command"], "node")
        self.assertEqual(data["mcpServers"]["clodex"]["command"], "clodex")
        self.assertEqual(data["mcpServers"]["clodex"]["args"], ["mcp-server"])

    def test_render_mcp_json_rejects_invalid_json_without_force(self):
        from clodex.native import render_mcp_json

        with self.assertRaises(ManagedBlockError):
            render_mcp_json("{not-json")

    def test_render_mcp_json_rejects_non_object_servers_without_force(self):
        from clodex.native import render_mcp_json

        with self.assertRaises(ManagedBlockError):
            render_mcp_json('{"mcpServers":[]}')

    def test_render_mcp_json_rejects_null_servers_without_force(self):
        from clodex.native import render_mcp_json

        with self.assertRaises(ManagedBlockError):
            render_mcp_json('{"mcpServers":null}')

    def test_render_mcp_json_force_replaces_invalid_json(self):
        from clodex.native import render_mcp_json

        data = json.loads(render_mcp_json("{not-json", force=True))
        self.assertEqual(data["mcpServers"]["clodex"]["command"], "clodex")

    def test_render_mcp_json_force_replaces_non_object_servers(self):
        from clodex.native import render_mcp_json

        data = json.loads(render_mcp_json('{"mcpServers":[]}', force=True))
        self.assertEqual(data["mcpServers"]["clodex"]["command"], "clodex")

    def test_render_mcp_json_preserves_crlf_style(self):
        from clodex.native import render_mcp_json

        rendered = render_mcp_json('{\r\n  "mcpServers": {}\r\n}\r\n')
        self.assertIn("\r\n", rendered)
        self.assertNotIn("\n", rendered.replace("\r\n", ""))
        self.assertTrue(rendered.endswith("\r\n"))

    def test_render_codex_toml_preserves_existing_config(self):
        from clodex.native import render_codex_toml

        rendered = render_codex_toml('model = "gpt-5.5"\n')
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.clodex]", rendered)
        self.assertIn('command = "clodex"', rendered)
        self.assertIn('args = ["mcp-server"]', rendered)

    def test_render_codex_toml_rejects_unmanaged_clodex_table_without_force(self):
        from clodex.native import render_codex_toml

        existing = 'model = "gpt-5.5"\n\n[mcp_servers.clodex]\ncommand = "old-clodex"\n'
        with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
            render_codex_toml(existing)

    def test_render_codex_toml_rejects_quoted_unmanaged_clodex_tables_without_force(self):
        from clodex.native import render_codex_toml

        for header in ('[mcp_servers."clodex"]', '["mcp_servers".clodex]', '["mcp_servers"."clodex"]'):
            with self.subTest(header=header):
                existing = f'model = "gpt-5.5"\n\n{header}\ncommand = "old-clodex"\n'
                with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
                    render_codex_toml(existing)

    def test_render_codex_toml_rejects_escaped_unmanaged_clodex_table_without_force(self):
        from clodex.native import render_codex_toml

        existing = '["mcp_servers"."clo\\u0064ex"]\ncommand = "old"\n'
        with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
            render_codex_toml(existing)

    def test_render_codex_toml_rejects_non_header_clodex_shapes_with_and_without_force(self):
        from clodex.native import render_codex_toml

        for existing in (
            'mcp_servers.clodex.command = "old"\n',
            '[mcp_servers]\nclodex = { command = "old" }\n',
            'mcp_servers = { clodex = { command = "old" } }\n',
        ):
            with self.subTest(existing=existing):
                with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
                    render_codex_toml(existing)
                with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
                    render_codex_toml(existing, force=True)

    def test_render_codex_toml_rejects_descendant_unmanaged_clodex_table_without_force(self):
        from clodex.native import render_codex_toml

        existing = '[mcp_servers.clodex.env]\nFOO = "old"\n'
        with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
            render_codex_toml(existing)

    def test_render_codex_toml_ignores_table_text_inside_multiline_basic_string(self):
        from clodex.native import render_codex_toml

        existing = 'notes = """\n[mcp_servers.clodex]\ncommand = "text only"\n"""\n'
        rendered = render_codex_toml(existing)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], '[mcp_servers.clodex]\ncommand = "text only"\n')
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertIn('[mcp_servers.clodex]\ncommand = "text only"\n', rendered)

    def test_render_codex_toml_force_ignores_table_text_inside_multiline_basic_string(self):
        from clodex.native import render_codex_toml

        existing = 'notes = """\n[mcp_servers.clodex]\ncommand = "text only"\n"""\n'
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], '[mcp_servers.clodex]\ncommand = "text only"\n')
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertIn('[mcp_servers.clodex]\ncommand = "text only"\n', rendered)

    def test_render_codex_toml_ignores_table_text_inside_multiline_literal_string(self):
        from clodex.native import render_codex_toml

        existing = "notes = '''\n[mcp_servers.clodex]\ncommand = \"text only\"\n'''\n"
        rendered = render_codex_toml(existing)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], '[mcp_servers.clodex]\ncommand = "text only"\n')
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertIn('[mcp_servers.clodex]\ncommand = "text only"\n', rendered)

    def test_render_codex_toml_preserves_multiline_basic_string_managed_markers(self):
        from clodex.native import render_codex_toml

        existing = 'notes = """\n# BEGIN CLODEX\nnot managed\n# END CLODEX\n"""\n'
        rendered = render_codex_toml(existing)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], "# BEGIN CLODEX\nnot managed\n# END CLODEX\n")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertEqual(rendered.count("# BEGIN CLODEX"), 2)

    def test_render_codex_toml_force_preserves_multiline_basic_string_managed_markers(self):
        from clodex.native import render_codex_toml

        existing = 'notes = """\n# BEGIN CLODEX\nnot managed\n# END CLODEX\n"""\n'
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], "# BEGIN CLODEX\nnot managed\n# END CLODEX\n")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertEqual(rendered.count("# BEGIN CLODEX"), 2)

    def test_render_codex_toml_preserves_multiline_literal_string_managed_markers(self):
        from clodex.native import render_codex_toml

        existing = "notes = '''\n# BEGIN CLODEX\nnot managed\n# END CLODEX\n'''\n"
        rendered = render_codex_toml(existing)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], "# BEGIN CLODEX\nnot managed\n# END CLODEX\n")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertEqual(rendered.count("# BEGIN CLODEX"), 2)

    def test_render_codex_toml_force_preserves_one_sided_marker_inside_multiline_string(self):
        from clodex.native import render_codex_toml

        existing = 'model = "gpt-5.5"\nnotes = """\n# BEGIN CLODEX\nnot managed\n"""\n'
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(data["model"], "gpt-5.5")
        self.assertEqual(data["notes"], "# BEGIN CLODEX\nnot managed\n")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")

    def test_render_codex_toml_real_malformed_managed_block_outside_string_rejects_and_force_repairs(self):
        from clodex.native import render_codex_toml

        existing = 'model = "gpt-5.5"\n# BEGIN CLODEX\nold = "value"\n'
        with self.assertRaises(ManagedBlockError):
            render_codex_toml(existing)
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(data["model"], "gpt-5.5")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertNotIn('old = "value"', rendered)

    def test_render_codex_toml_force_adopts_unmanaged_clodex_table(self):
        from clodex.native import render_codex_toml

        existing = (
            'model = "gpt-5.5"\n'
            "\n"
            "[mcp_servers.clodex]\n"
            'command = "old-clodex"\n'
            'args = ["old"]\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "other"\n'
        )
        rendered = render_codex_toml(existing, force=True)
        self.assertEqual(rendered.count("[mcp_servers.clodex]"), 1)
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.other]", rendered)
        self.assertIn('command = "other"', rendered)
        self.assertEqual(tomllib.loads(rendered)["mcp_servers"]["clodex"]["command"], "clodex")

    def test_render_codex_toml_force_adopts_descendant_unmanaged_clodex_tables(self):
        from clodex.native import render_codex_toml

        existing = (
            'model = "gpt-5.5"\n'
            "\n"
            "[mcp_servers.clodex]\n"
            'command = "old-clodex"\n'
            "\n"
            "[mcp_servers.clodex.env]\n"
            'FOO = "old"\n'
            "\n"
            "[mcp_servers.clodex.env.nested]\n"
            'BAR = "old"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "other"\n'
        )
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(rendered.count("[mcp_servers.clodex]"), 1)
        self.assertNotIn("[mcp_servers.clodex.env]", rendered)
        self.assertNotIn("[mcp_servers.clodex.env.nested]", rendered)
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.other]", rendered)
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertNotIn("env", data["mcp_servers"]["clodex"])
        self.assertEqual(data["mcp_servers"]["other"]["command"], "other")

    def test_render_codex_toml_force_adopts_quoted_unmanaged_clodex_tables(self):
        from clodex.native import render_codex_toml

        for header in ('[mcp_servers."clodex"]', '["mcp_servers".clodex]', '["mcp_servers"."clodex"]'):
            with self.subTest(header=header):
                existing = (
                    'model = "gpt-5.5"\r\n'
                    "\r\n"
                    f"{header}\r\n"
                    'command = "old-clodex"\r\n'
                    'args = ["old"]\r\n'
                    "\r\n"
                    "[mcp_servers.other]\r\n"
                    'command = "other"\r\n'
                )
                rendered = render_codex_toml(existing, force=True)
                data = tomllib.loads(rendered)
                self.assertEqual(rendered.count("[mcp_servers.clodex]"), 1)
                self.assertNotIn(header, rendered)
                self.assertIn('model = "gpt-5.5"', rendered)
                self.assertIn("[mcp_servers.other]", rendered)
                self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
                self.assertEqual(data["mcp_servers"]["other"]["command"], "other")
                self.assertIn("# BEGIN CLODEX\r\n", rendered)

    def test_render_codex_toml_force_adopts_escaped_unmanaged_clodex_table(self):
        from clodex.native import render_codex_toml

        existing = (
            'model = "gpt-5.5"\r\n'
            "\r\n"
            '["mcp_servers"."clo\\u0064ex"]\r\n'
            'command = "old"\r\n'
            "\r\n"
            "[mcp_servers.other]\r\n"
            'command = "other"\r\n'
        )
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(rendered.count("[mcp_servers.clodex]"), 1)
        self.assertNotIn('["mcp_servers"."clo\\u0064ex"]', rendered)
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.other]", rendered)
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertEqual(data["mcp_servers"]["other"]["command"], "other")
        self.assertIn("# BEGIN CLODEX\r\n", rendered)

    def test_render_codex_toml_rejects_invalid_unrelated_toml_without_force(self):
        from clodex.native import render_codex_toml

        existing = (
            "[mcp_servers.other]\n"
            'command = "one"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "two"\n'
        )
        with self.assertRaisesRegex(ManagedBlockError, "Invalid .codex/config.toml"):
            render_codex_toml(existing)

    def test_render_codex_toml_rejects_invalid_unrelated_toml_with_force(self):
        from clodex.native import render_codex_toml

        existing = (
            "[mcp_servers.other]\n"
            'command = "one"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "two"\n'
        )
        with self.assertRaisesRegex(ManagedBlockError, "Invalid .codex/config.toml"):
            render_codex_toml(existing, force=True)
