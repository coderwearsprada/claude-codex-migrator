"""
Tests for migrate.py.

Run with:
    python3 -m unittest discover -s tests
    # or
    python3 tests/test_migrate.py

Uses only the standard library (matches the migrator's stdlib-only stance).
Works on Python 3.9+ (uses migrate.py's TOML reader fallback when stdlib
`tomllib` isn't available).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Make migrate.py importable when run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import migrate as m  # noqa: E402

# Use whichever TOML reader migrate.py chose (stdlib on 3.11+,
# hand-rolled fallback on 3.9/3.10). Both expose `.loads(text)`.
tomllib = m.tomllib


def make_ctx(src: Path, dst: Path, **overrides) -> m.Ctx:
    """Build a Ctx for tests. plan_mode=False, dry_run=False, backup=True by default."""
    defaults = dict(
        src_root=src,
        dst_root=dst,
        src_doc=None,
        dst_doc=None,
        dry_run=False,
        merge=True,
        backup=True,
        report=m.Report(direction="test"),
    )
    defaults.update(overrides)
    return m.Ctx(**defaults)


# ============================================================================
# Pure-function tests (no filesystem)
# ============================================================================

class TomlWriterTests(unittest.TestCase):
    """The migrator ships its own TOML writer (stdlib is read-only).
    Tests verify it produces output tomllib can parse back to the same dict."""

    def test_roundtrip_scalars(self):
        data = {"model": "gpt-5", "approval_policy": "never",
                "max_turns": 5, "verbose": True}
        self.assertEqual(tomllib.loads(m.render_toml(data)), data)

    def test_roundtrip_nested_tables(self):
        data = {
            "model": "gpt-5",
            "mcp_servers": {
                "foo": {"command": "fooserver", "args": ["--x"],
                        "env": {"K": "v"}},
            },
            "shell_environment_policy": {"set": {"A": "1", "B": "2"}},
        }
        self.assertEqual(tomllib.loads(m.render_toml(data)), data)

    def test_escapes_quotes_and_backslashes(self):
        data = {"path": 'C:\\Users\\me', "msg": 'said "hi"'}
        self.assertEqual(tomllib.loads(m.render_toml(data)), data)

    def test_array_of_strings(self):
        data = {"notify": ["/bin/echo", "ding", "dong"]}
        self.assertEqual(tomllib.loads(m.render_toml(data)), data)


class FrontmatterTests(unittest.TestCase):
    """YAML-like frontmatter parsing + the round-trip-able meta-comment encoding."""

    def test_strip_returns_body_and_dict(self):
        body, fm = m.strip_frontmatter("---\nfoo: bar\nbaz: 1\n---\nbody\n")
        self.assertEqual(body, "body\n")
        self.assertEqual(fm, {"foo": "bar", "baz": "1"})

    def test_strip_missing_returns_none_dict(self):
        body, fm = m.strip_frontmatter("no frontmatter here\n")
        self.assertIsNone(fm)
        self.assertEqual(body, "no frontmatter here\n")

    def test_meta_comment_roundtrip(self):
        fm = {"description": "do a thing", "argument-hint": "<arg>"}
        text = m.frontmatter_to_meta_comment(fm) + "rest\n"
        rest, parsed = m.meta_comment_to_frontmatter(text)
        self.assertEqual(rest, "rest\n")
        self.assertEqual(parsed, fm)

    def test_meta_comment_only_carries_supported_keys(self):
        # description + argument-hint round-trip; model + allowed-tools don't.
        fm = {"description": "d", "model": "opus", "allowed-tools": "Bash"}
        comment = m.frontmatter_to_meta_comment(fm)
        self.assertIn('description="d"', comment)
        self.assertNotIn("model=", comment)
        self.assertNotIn("allowed-tools=", comment)


class FencedBlockTests(unittest.TestCase):
    """Migrator uses <!-- migrator:begin/end --> fenced blocks to round-trip
    content like Codex `instructions` or Claude `outputStyle` files."""

    def test_encode_then_extract(self):
        text = "preamble\n" + m.fenced_block("outputStyle", "concise", "Be terse.")
        cleaned, body = m.extract_fenced(text, "outputStyle")
        self.assertEqual(body, "Be terse.")
        self.assertNotIn("migrator:begin", cleaned)

    def test_extract_missing_returns_none(self):
        cleaned, body = m.extract_fenced("nothing here", "outputStyle")
        self.assertIsNone(body)
        self.assertEqual(cleaned, "nothing here")

    def test_only_first_match_of_kind_returned(self):
        text = (m.fenced_block("k", "a", "first") +
                m.fenced_block("k", "b", "second"))
        _, body = m.extract_fenced(text, "k")
        self.assertEqual(body, "first")


class EffortMappingTests(unittest.TestCase):
    def test_claude_to_codex_caps_at_high(self):
        # Claude `max` has no Codex equivalent; it collapses to `high`.
        self.assertEqual(m.EFFORT_C2X["max"], "high")
        self.assertEqual(m.EFFORT_C2X["low"], "low")

    def test_codex_minimal_maps_to_low(self):
        # Codex `minimal` has no Claude equivalent; it collapses to `low`.
        self.assertEqual(m.EFFORT_X2C["minimal"], "low")
        self.assertEqual(m.EFFORT_X2C["high"], "high")


class McpNormalizeTests(unittest.TestCase):
    def test_stdio_server_passes(self):
        report = m.Report("test")
        out = m._normalize_mcp_claude_to_codex(
            "foo", {"command": "x", "args": ["a"], "env": {"K": "v"}}, report)
        self.assertEqual(out, {"command": "x", "args": ["a"], "env": {"K": "v"}})

    def test_sse_server_skipped_with_report_note(self):
        report = m.Report("test")
        out = m._normalize_mcp_claude_to_codex(
            "foo", {"type": "sse", "url": "https://x"}, report)
        self.assertIsNone(out)
        self.assertTrue(any("sse" in s for s in report.skipped_unmappable))

    def test_command_missing_skipped(self):
        report = m.Report("test")
        out = m._normalize_mcp_claude_to_codex("foo", {}, report)
        self.assertIsNone(out)

    def test_codex_to_claude_injects_stdio_type(self):
        out = m._normalize_mcp_codex_to_claude({"command": "x", "args": ["a"]})
        self.assertEqual(out["type"], "stdio")
        self.assertEqual(out["command"], "x")


# ============================================================================
# Filesystem tests (real tmpdirs, no subprocesses)
# ============================================================================

class FsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="migrate-test-"))
        self.src = self.tmp / "src"
        self.dst = self.tmp / "dst"
        self.src.mkdir()
        self.dst.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TierASettingsClaudeToCodexTests(FsTestBase):
    def test_all_clean_fields_translate(self):
        (self.src / "settings.json").write_text(json.dumps({
            "model": "claude-opus",
            "mcpServers": {"foo": {"command": "fooserver", "args": ["--x"]}},
            "env": {"API_KEY": "secret"},
            "effortLevel": "max",
        }))
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_settings_claude_to_codex(ctx)

        cfg = tomllib.loads((self.dst / "config.toml").read_text())
        self.assertEqual(cfg["model"], "claude-opus")
        self.assertEqual(cfg["mcp_servers"]["foo"]["command"], "fooserver")
        self.assertEqual(cfg["shell_environment_policy"]["set"]["API_KEY"], "secret")
        self.assertEqual(cfg["model_reasoning_effort"], "high")

    def test_unmappable_keys_are_reported(self):
        (self.src / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": "echo hi"},
            "theme": "dark",
        }))
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_settings_claude_to_codex(ctx)
        self.assertTrue(any("statusLine" in s for s in ctx.report.skipped_unmappable))
        self.assertTrue(any("theme" in s for s in ctx.report.skipped_unmappable))


class TierASettingsCodexToClaudeTests(FsTestBase):
    def test_all_clean_fields_translate(self):
        (self.src / "config.toml").write_text(
            'model = "gpt-5"\n'
            'model_reasoning_effort = "minimal"\n'
            '[mcp_servers.foo]\n'
            'command = "fooserver"\n'
            'args = ["--x"]\n'
            '[shell_environment_policy]\n'
            'set = { API_KEY = "secret" }\n'
        )
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_settings_codex_to_claude(ctx)

        s = json.loads((self.dst / "settings.json").read_text())
        self.assertEqual(s["model"], "gpt-5")
        self.assertEqual(s["mcpServers"]["foo"]["command"], "fooserver")
        self.assertEqual(s["mcpServers"]["foo"]["type"], "stdio")
        self.assertEqual(s["env"]["API_KEY"], "secret")
        self.assertEqual(s["effortLevel"], "low")


class CommandsRoundTripTests(FsTestBase):
    """Verify round-trip preservation of supported frontmatter keys."""

    def test_description_and_argument_hint_round_trip(self):
        cmds = self.src / "commands"
        cmds.mkdir()
        (cmds / "x.md").write_text(
            "---\ndescription: do things\nargument-hint: <x>\n---\nbody text\n")

        ctx_c2x = make_ctx(self.src, self.dst)
        m.tier_a_commands_to_prompts(ctx_c2x)

        # Now go back: dst (with prompts) → dst2 (which gets commands).
        dst2 = self.tmp / "dst2"
        dst2.mkdir()
        ctx_x2c = make_ctx(self.dst, dst2)
        m.tier_a_prompts_to_commands(ctx_x2c)

        result = (dst2 / "commands" / "x.md").read_text()
        body, fm = m.strip_frontmatter(result)
        self.assertEqual(fm, {"description": "do things", "argument-hint": "<x>"})
        self.assertEqual(body.strip(), "body text")

    def test_unsupported_frontmatter_keys_noted(self):
        cmds = self.src / "commands"
        cmds.mkdir()
        (cmds / "foo.md").write_text(
            "---\ndescription: D\nmodel: opus\nallowed-tools: Bash\n---\nbody\n")

        ctx = make_ctx(self.src, self.dst)
        m.tier_a_commands_to_prompts(ctx)

        # description rides along in the meta comment; model + allowed-tools do not.
        out = (self.dst / "prompts" / "foo.md").read_text()
        self.assertIn('description="D"', out)
        self.assertNotIn("allowed-tools", out)
        self.assertTrue(any("model" in n and "allowed-tools" in n
                            for n in ctx.report.notes))


class TierBPermissionsToSandboxTests(FsTestBase):
    """Verify the heuristic that maps Claude's per-tool patterns to Codex's
    coarse sandbox modes. Round-trip is not exact (lossy by design)."""

    def _run(self, permissions):
        (self.src / "settings.json").write_text(
            json.dumps({"permissions": permissions}))
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_permissions(ctx)
        return tomllib.loads((self.dst / "config.toml").read_text())

    def test_bash_star_with_no_deny_is_full_access(self):
        cfg = self._run({"allow": ["Bash(*)"]})
        self.assertEqual(cfg["approval_policy"], "never")
        self.assertEqual(cfg["sandbox_mode"], "danger-full-access")

    def test_reads_only_is_read_only_sandbox(self):
        cfg = self._run({"allow": ["Read(*)"]})
        self.assertEqual(cfg["sandbox_mode"], "read-only")

    def test_write_patterns_become_writable_roots(self):
        cfg = self._run({"allow": ["Read(*)", "Write(./src/**)",
                                   "Write(./tests/**)"]})
        roots = cfg["sandbox_workspace_write"]["writable_roots"]
        self.assertIn("./src", roots)
        self.assertIn("./tests", roots)

    def test_webfetch_deny_disables_network(self):
        cfg = self._run({"allow": ["Read(*)", "Write(./src/**)"],
                         "deny": ["WebFetch(*)"]})
        self.assertFalse(cfg["sandbox_workspace_write"]["network_access"])


class TierBSandboxToPermissionsTests(FsTestBase):
    def _run(self, toml_body):
        (self.src / "config.toml").write_text(toml_body)
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_sandbox(ctx)
        return json.loads((self.dst / "settings.json").read_text())

    def test_danger_full_expands_to_wildcards(self):
        s = self._run('sandbox_mode = "danger-full-access"\n')
        self.assertIn("Bash(*)", s["permissions"]["allow"])

    def test_read_only_denies_writes_and_bash(self):
        s = self._run('sandbox_mode = "read-only"\n')
        self.assertIn("Read(*)", s["permissions"]["allow"])
        self.assertIn("Write(*)", s["permissions"]["deny"])
        self.assertIn("Bash(*)", s["permissions"]["deny"])

    def test_workspace_write_with_roots_and_no_network(self):
        s = self._run(
            'sandbox_mode = "workspace-write"\n'
            '[sandbox_workspace_write]\n'
            'writable_roots = ["./src", "./tests"]\n'
            'network_access = false\n'
        )
        self.assertIn("Write(./src/**)", s["permissions"]["allow"])
        self.assertIn("Write(./tests/**)", s["permissions"]["allow"])
        self.assertIn("WebFetch(*)", s["permissions"]["deny"])


class TierBHooksNotifyTests(FsTestBase):
    def test_claude_notification_hook_becomes_notify(self):
        (self.src / "settings.json").write_text(json.dumps({
            "hooks": {
                "Notification": [{"hooks": [
                    {"type": "command", "command": "say hello"}
                ]}],
                "PreToolUse": [{"hooks": [
                    {"type": "command", "command": "echo pre"}
                ]}],
            }
        }))
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_notify_hook(ctx)

        cfg = tomllib.loads((self.dst / "config.toml").read_text())
        # Codex notify takes an argv list; we wrap the shell string in /bin/sh -c.
        self.assertEqual(cfg["notify"], ["/bin/sh", "-c", "say hello"])
        self.assertTrue(any("PreToolUse" in s
                            for s in ctx.report.skipped_unmappable))

    def test_codex_notify_becomes_notification_hook(self):
        (self.src / "config.toml").write_text(
            'notify = ["/bin/echo", "ding"]\n')
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_notify(ctx)

        s = json.loads((self.dst / "settings.json").read_text())
        self.assertIn("Notification", s["hooks"])


class TierBAgentsAndSkillsTests(FsTestBase):
    def test_subagent_becomes_prefixed_prompt(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text(
            "---\nname: reviewer\n---\nDo a review.\n")
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_agents(ctx)

        out = (self.dst / "prompts" / "agent-reviewer.md").read_text()
        self.assertIn("Do a review.", out)
        self.assertIn("Claude subagent", out)

    def test_skill_flattens_and_notes_assets(self):
        sk = self.src / "skills" / "myskill"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text(
            "---\nname: myskill\n---\nSkill content.\n")
        (sk / "asset.txt").write_text("asset bytes")

        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_skills(ctx)

        out = (self.dst / "prompts" / "skill-myskill.md").read_text()
        self.assertIn("Skill content.", out)
        self.assertIn("1 asset file(s)", out)


# ============================================================================
# Plan-then-apply + backup + restore
# ============================================================================

class PlanModeTests(FsTestBase):
    def test_plan_records_writes_without_creating_files(self):
        (self.src / "settings.json").write_text(json.dumps({"model": "x"}))
        ctx = make_ctx(self.src, self.dst, plan_mode=True)
        m.tier_a_settings_claude_to_codex(ctx)

        self.assertIn(self.dst / "config.toml", ctx.planned_writes)
        self.assertFalse((self.dst / "config.toml").exists())

    def test_apply_writes(self):
        (self.src / "settings.json").write_text(json.dumps({"model": "x"}))
        ctx = make_ctx(self.src, self.dst)  # plan_mode=False
        m.tier_a_settings_claude_to_codex(ctx)
        self.assertTrue((self.dst / "config.toml").exists())


class BackupAndRestoreTests(FsTestBase):
    def test_perform_backup_writes_manifest_and_files(self):
        (self.dst / "settings.json").write_text('{"model": "original"}')

        ctx = make_ctx(self.src, self.dst)
        ctx.planned_writes = {
            self.dst / "settings.json",
            self.dst / "new_file.json",
        }
        backup_root = m.perform_backup(ctx)
        self.assertIsNotNone(backup_root)

        manifest = json.loads((backup_root / "manifest.json").read_text())
        entries = {e["relative"]: e for e in manifest["entries"]}
        self.assertTrue(entries["settings.json"]["existed_before"])
        self.assertFalse(entries["new_file.json"]["existed_before"])
        # The pre-migration content is in the backup byte-for-byte.
        self.assertEqual((backup_root / "settings.json").read_text(),
                         '{"model": "original"}')

    def test_full_migrate_then_restore_round_trip(self):
        # Pre-existing settings.json that the migration will modify.
        (self.dst / "settings.json").write_text('{"model": "original"}')

        ctx = make_ctx(self.src, self.dst)
        ctx.planned_writes = {
            self.dst / "settings.json",
            self.dst / "fresh.json",
        }
        backup_root = m.perform_backup(ctx)

        # Simulate the migration's effects.
        (self.dst / "settings.json").write_text('{"model": "modified"}')
        (self.dst / "fresh.json").write_text("{}")

        rc = m.restore_from_backup(
            backup_root, interactive=False, dry_run=False)
        self.assertEqual(rc, 0)

        # Pre-existing file restored byte-for-byte; freshly-created one gone.
        self.assertEqual((self.dst / "settings.json").read_text(),
                         '{"model": "original"}')
        self.assertFalse((self.dst / "fresh.json").exists())


# ============================================================================
# Cursor support
# ============================================================================

class CursorMcpTests(FsTestBase):
    def test_read_mcp_json(self):
        (self.src / "mcp.json").write_text(json.dumps({
            "mcpServers": {"foo": {"command": "fooserver", "args": ["--x"]}}
        }))
        servers = m.cursor_read_mcp(self.src)
        self.assertEqual(servers["foo"]["command"], "fooserver")

    def test_write_mcp_merges_existing(self):
        (self.dst / "mcp.json").write_text(json.dumps({
            "mcpServers": {"keep": {"command": "k"}}
        }))
        ctx = make_ctx(self.src, self.dst)
        m.cursor_write_mcp({"new": {"command": "n"}}, self.dst, ctx)
        data = json.loads((self.dst / "mcp.json").read_text())
        self.assertEqual(set(data["mcpServers"].keys()), {"keep", "new"})


class CursorRulesTests(FsTestBase):
    def test_mdc_round_trip_preserves_globs_and_alwaysapply(self):
        rules_dir = self.src / "rules"
        rules_dir.mkdir()
        (rules_dir / "react.mdc").write_text(
            "---\ndescription: React rules\nglobs: src/**/*.tsx\n"
            "alwaysApply: false\n---\nUse hooks.\n")
        (rules_dir / "general.mdc").write_text(
            "---\ndescription: General\nalwaysApply: true\n---\nBe concise.\n")

        rules = m.cursor_read_rules(self.src)
        # Order: directory listing is sorted, so general before react.
        names = sorted(r.name for r in rules)
        self.assertEqual(names, ["general", "react"])

        # Render to a fenced doc, parse back, write fresh rules — should
        # reproduce the originals.
        doc = m.cursor_rules_to_doc(rules)
        parsed = m.doc_to_cursor_rules(doc)
        # 2 fenced rules + possibly a default "migrated" if leftover text;
        # there's no leftover in this case.
        by_name = {r.name: r for r in parsed}
        self.assertIn("react", by_name)
        self.assertEqual(by_name["react"].globs, "src/**/*.tsx")
        self.assertFalse(by_name["react"].always_apply)
        self.assertEqual(by_name["general"].body, "Be concise.")
        self.assertTrue(by_name["general"].always_apply)

    def test_legacy_cursorrules_picked_up(self):
        # Simulate <project_root>/.cursorrules next to a .cursor/ dir.
        project_root = self.tmp / "proj"
        project_root.mkdir()
        cursor_root = project_root / ".cursor"
        cursor_root.mkdir()
        (project_root / ".cursorrules").write_text("Use 2-space indent.\n")

        rules = m.cursor_read_rules(cursor_root)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].name, "_cursorrules_legacy")
        self.assertIn("2-space", rules[0].body)

    def test_doc_with_leftover_becomes_default_rule(self):
        text = ("# Top-level notes\nBe nice.\n\n" +
                m.cursor_rules_to_doc([
                    m.CursorRule(name="r1", description="", globs=None,
                                 always_apply=True, body="rule one body")]))
        rules = m.doc_to_cursor_rules(text, default_name="leftover")
        names = {r.name for r in rules}
        self.assertEqual(names, {"r1", "leftover"})
        leftover = next(r for r in rules if r.name == "leftover")
        self.assertIn("Be nice", leftover.body)


class CursorToolPathsTests(unittest.TestCase):
    def test_cursor_paths_user_scope(self):
        p = m._tool_paths("cursor", "user", None)
        self.assertEqual(p["root"], Path.home() / ".cursor")
        self.assertIsNone(p["doc"])

    def test_cursor_paths_project_scope(self):
        p = m._tool_paths("cursor", "project", None)
        self.assertEqual(p["root"], Path.cwd() / ".cursor")
        self.assertEqual(p["doc"], Path.cwd() / ".cursorrules")

    def test_override_dir_short_circuits_scope(self):
        p = m._tool_paths("cursor", "user", "/tmp/somewhere")
        self.assertEqual(p["root"], Path("/tmp/somewhere"))


class CursorDirectionDriversTests(FsTestBase):
    def test_claude_to_cursor_creates_rules_and_mcp(self):
        (self.src / "settings.json").write_text(json.dumps({
            "mcpServers": {"foo": {"command": "fooserver"}}
        }))
        (self.src / "CLAUDE.md").write_text("# Be concise\n")

        ctx = make_ctx(self.src, self.dst)
        m.run_claude_to_cursor(ctx, {})

        self.assertTrue((self.dst / "mcp.json").exists())
        mdc = list((self.dst / "rules").glob("*.mdc"))
        self.assertEqual(len(mdc), 1)
        self.assertIn("Be concise", mdc[0].read_text())

    def test_cursor_to_claude_round_trips_rules(self):
        rules_dir = self.src / "rules"
        rules_dir.mkdir()
        (rules_dir / "r1.mdc").write_text(
            "---\ndescription: R1\nglobs: src/**\nalwaysApply: false\n---\n"
            "Rule one.\n")
        (self.src / "mcp.json").write_text(json.dumps({
            "mcpServers": {"foo": {"command": "fooserver"}}}))

        ctx = make_ctx(self.src, self.dst)
        m.run_cursor_to_claude(ctx, {})

        text = (self.dst / "CLAUDE.md").read_text()
        self.assertIn("migrator:begin kind=cursor-rule source=r1", text)
        self.assertIn('globs="src/**"', text)
        s = json.loads((self.dst / "settings.json").read_text())
        self.assertEqual(s["mcpServers"]["foo"]["type"], "stdio")

    def test_cursor_to_codex_filters_non_stdio_mcp(self):
        (self.src / "mcp.json").write_text(json.dumps({
            "mcpServers": {
                "ok":  {"command": "x"},
                "bad": {"type": "sse", "url": "https://x"},
            }
        }))
        ctx = make_ctx(self.src, self.dst)
        m.run_cursor_to_codex(ctx, {})

        cfg = tomllib.loads((self.dst / "config.toml").read_text())
        self.assertIn("ok", cfg["mcp_servers"])
        self.assertNotIn("bad", cfg["mcp_servers"])
        self.assertTrue(any("sse" in s for s in ctx.report.skipped_unmappable))

    def test_codex_to_cursor_translates_mcp_and_docs(self):
        (self.src / "config.toml").write_text(
            'model = "gpt-5"\n'
            '[mcp_servers.foo]\ncommand = "fooserver"\n'
        )
        (self.src / "AGENTS.md").write_text("# Test rules\n")
        ctx = make_ctx(self.src, self.dst)
        m.run_codex_to_cursor(ctx, {})

        data = json.loads((self.dst / "mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["foo"]["command"], "fooserver")
        mdcs = list((self.dst / "rules").glob("*.mdc"))
        self.assertEqual(len(mdcs), 1)


class CursorFullRoundTripTests(FsTestBase):
    def test_cursor_to_claude_to_cursor_preserves_rule_metadata(self):
        # Original cursor rules with globs + alwaysApply.
        c1 = self.tmp / "c1"
        rules_c1 = c1 / "rules"
        rules_c1.mkdir(parents=True)
        (rules_c1 / "react.mdc").write_text(
            "---\ndescription: React rules\nglobs: src/**/*.tsx\n"
            "alwaysApply: false\n---\nUse hooks.\n")

        # cursor → claude
        cl = self.tmp / "claude"
        cl.mkdir()
        ctx1 = make_ctx(c1, cl)
        m.run_cursor_to_claude(ctx1, {})

        # claude → cursor (different dst dir to avoid the original)
        c2 = self.tmp / "c2"
        c2.mkdir()
        ctx2 = make_ctx(cl, c2)
        m.run_claude_to_cursor(ctx2, {})

        # The react rule's frontmatter should round-trip byte-equivalent.
        result = (c2 / "rules" / "react.mdc").read_text()
        body, fm = m.strip_frontmatter(result)
        self.assertEqual(fm.get("globs"), "src/**/*.tsx")
        self.assertEqual(fm.get("alwaysApply"), "false")
        self.assertEqual(body.strip(), "Use hooks.")


if __name__ == "__main__":
    unittest.main()
