"""Registry staleness: what must (and must not) make a built registry stale.

The known field issue was a registry that went stale "every now and then" and
needed a manual rebuild. Three causes, each pinned here against the real rebuild
and freshness code on a temporary HOME:

- Harness config files carry state the harness rewrites on its own (a project
  entry per directory Claude Code opens, Codex project trust and model choice,
  Claude settings unrelated to capabilities). Hashing them whole made the
  registry stale for changes discovery never reads.
- New, removed, or updated skills and plugins were invisible to queries (the
  deep walk was too slow for the query path), so routing silently missed them
  until someone rebuilt by hand.
- Self-heal always re-ran every harness CLI and failed the query outright when
  one of them was slow or broken, even when a plain rebuild was all it needed.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402
import router_config  # noqa: E402

OLD_NS = time.time_ns() - 3_600 * 1_000_000_000


def age(*paths: Path) -> None:
    """Backdate fixture directories so a later change always moves their mtime."""
    for path in paths:
        os.utime(path, ns=(OLD_NS, OLD_NS))


class FreshnessFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_config = registry.ROUTER_CONFIG

    @classmethod
    def tearDownClass(cls) -> None:
        registry.configure_router(cls._original_config)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lockkeeper-freshness-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name).resolve(strict=False)
        self.home = self.temp / "home"
        self.home.mkdir()
        environment = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home), "CAPABILITY_ROUTER_CONFIG": ""},
        )
        environment.start()
        self.addCleanup(environment.stop)
        registry._EMITTED_WARNINGS.clear()
        self.addCleanup(registry._EMITTED_WARNINGS.clear)

        script_path = self.temp / "router" / "scripts" / "capability_registry.py"
        script_path.parent.mkdir(parents=True)
        script_path.touch()
        self.project = self.temp / "project"
        self.project.mkdir()
        self.output = self.temp / "output"
        builtin = router_config.load_router_config(
            script_path=script_path, include_repository=False, include_explicit=False
        )
        registry.configure_router(
            replace(
                builtin,
                output_dir=self.output,
                snapshot_dir=self.temp / "snapshots",
                cwd=self.project,
                first_party_roots=(self.temp / "router",),
            )
        )
        # Module-level roots are computed from the HOME seen at import time.
        for name, value in (
            ("AGENT_ROOTS", [("claude", self.home / ".claude" / "agents", "*.md")]),
            ("COMMAND_ROOTS", [("claude", self.home / ".claude" / "commands")]),
            ("PLUGIN_CACHE_ROOTS", [("claude", self.home / ".claude" / "plugins" / "cache")]),
        ):
            patcher = mock.patch.object(registry, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        registry.TOOL_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        for path, payload in (
            (registry.TOOL_SNAPSHOT, {"tools": []}),
            (registry.CLAUDE_MCP_SNAPSHOT, {"servers": []}),
            (registry.CODEX_MCP_SNAPSHOT, {"servers": []}),
            (registry.PLUGIN_SNAPSHOT, {"plugins": {}}),
            (registry.HERMES_TOOL_SNAPSHOT, {"toolsets": [], "mcp_servers": []}),
        ):
            path.write_text(json.dumps({"schema_version": 1, **payload}), encoding="utf-8")
            os.utime(path, ns=(OLD_NS, OLD_NS))

        self.skills = self.home / ".agents" / "skills"
        self.write_skill(self.skills / "engineering" / "api-migration", "api-migration", "migrate api tokens")
        self.write_skill(self.skills / "engineering" / "webhook-audit", "webhook-audit", "payment webhook races")

    def write_skill(self, folder: Path, name: str, description: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        skill = folder / "SKILL.md"
        skill.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n", encoding="utf-8")
        return skill

    def write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def build(self) -> None:
        for path in self.home.rglob("*"):
            if path.is_dir():
                age(path)
        age(self.home)
        registry.rebuild(self.output, quiet=True)

    def assert_fresh(self) -> list[dict]:
        return registry.assert_registry_fresh(self.output, deep=False)

    def assert_stale(self, prefix: str) -> RuntimeError:
        with self.assertRaises(RuntimeError) as caught:
            registry.assert_registry_fresh(self.output, deep=False)
        self.assertTrue(str(caught.exception).startswith(prefix), str(caught.exception))
        self.assertTrue(registry.auto_refreshable_staleness(caught.exception))
        return caught.exception


class HarnessConfigNoiseTest(FreshnessFixture):
    def test_opening_new_projects_in_claude_code_keeps_the_registry_fresh(self) -> None:
        claude_json = self.home / ".claude.json"
        self.write_json(claude_json, {"numStartups": 1, "mcpServers": {"context7": {"command": "npx"}}})
        self.build()
        self.assert_fresh()

        # What Claude Code writes when it is opened in another directory.
        self.write_json(
            claude_json,
            {
                "numStartups": 2,
                "tipsHistory": {"x": 1},
                "mcpServers": {"context7": {"command": "npx"}},
                "projects": {
                    str(self.temp / "some-other-repo"): {
                        "allowedTools": [],
                        "mcpServers": {},
                        "enabledMcpjsonServers": [],
                        "disabledMcpjsonServers": [],
                        "hasTrustDialogAccepted": True,
                    }
                },
            },
        )
        self.assert_fresh()

        # A local-scope MCP for the router's own cwd is real capability config.
        self.write_json(
            claude_json,
            {
                "mcpServers": {"context7": {"command": "npx"}},
                "projects": {str(self.project): {"mcpServers": {"local-db": {"command": "db"}}}},
            },
        )
        self.assert_stale("Runtime configuration changed")

    def test_codex_project_trust_and_model_choice_keep_the_registry_fresh(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8")
        self.build()

        config.write_text(
            'model = "gpt-5"\nmodel_reasoning_effort = "high"\n\n'
            '[mcp_servers.context7]\ncommand = "npx"\n\n'
            f'[projects."{self.project.as_posix()}"]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        self.assert_fresh()

        config.write_text(
            '[mcp_servers.context7]\ncommand = "npx"\n\n[mcp_servers.github]\ncommand = "gh-mcp"\n',
            encoding="utf-8",
        )
        self.assert_stale("Runtime configuration changed")

    def test_claude_settings_outside_capabilities_keep_the_registry_fresh(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        self.write_json(settings, {"model": "opus", "enabledPlugins": {}})
        self.build()

        self.write_json(settings, {"model": "sonnet", "permissions": {"allow": ["Bash(ls)"]}, "enabledPlugins": {}})
        self.assert_fresh()

        self.write_json(settings, {"model": "sonnet", "enabledPlugins": {"demo@market": True}})
        self.assert_stale("Runtime configuration changed")

    def test_unparseable_config_still_fails_closed(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("model = [\n", encoding="utf-8")
        self.build()
        config.write_text("model = [ \n", encoding="utf-8")
        self.assert_stale("Runtime configuration changed")

    def test_plugin_mcp_configs_come_only_from_plugin_roots(self) -> None:
        cache = self.home / ".claude" / "plugins" / "cache"
        plugin_root = cache / "market" / "demo" / "1.0.0"
        self.write_json(plugin_root / ".claude-plugin" / "plugin.json", {"name": "demo"})
        self.write_json(plugin_root / ".mcp.json", {"mcpServers": {"demo": {}}})
        self.write_json(plugin_root / "node_modules" / "dep" / ".mcp.json", {"mcpServers": {"noise": {}}})
        self.write_json(cache / "stray" / "node_modules" / "x" / "mcp.json", {})

        self.assertEqual(registry.plugin_mcp_config_files(cache), [plugin_root / ".mcp.json"])


class DiscoveryWatchTest(FreshnessFixture):
    def test_a_newly_installed_skill_is_noticed_by_the_next_query(self) -> None:
        self.build()
        self.assert_fresh()
        self.write_skill(self.skills / "engineering" / "rate-limiter", "rate-limiter", "rest rate limiting")
        self.assert_stale("Registry skill discovery is stale")

    def test_a_new_top_level_skill_pack_is_noticed(self) -> None:
        self.build()
        self.write_skill(self.skills / "german-law" / "band-001" / "shard-1", "de-law-1", "BGB Widerruf")
        self.assert_stale("Registry skill discovery is stale")

    def test_a_removed_skill_is_noticed(self) -> None:
        self.build()
        target = self.skills / "engineering" / "webhook-audit"
        (target / "SKILL.md").unlink()
        target.rmdir()
        self.assert_stale("Registry skill discovery is stale")

    def test_a_new_agent_file_is_noticed(self) -> None:
        agents = self.home / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")
        self.build()
        (agents / "tester.md").write_text("# tester\n", encoding="utf-8")
        self.assert_stale("Registry skill discovery is stale")

    def test_a_plugin_version_update_is_noticed(self) -> None:
        plugin = self.home / ".claude" / "plugins" / "cache" / "market" / "demo"
        self.write_json(plugin / "1.0.0" / ".claude-plugin" / "plugin.json", {"name": "demo"})
        self.write_skill(plugin / "1.0.0" / "skills" / "demo-skill", "demo-skill", "demo")
        self.build()
        self.write_json(plugin / "1.1.0" / ".claude-plugin" / "plugin.json", {"name": "demo"})
        self.assert_stale("Registry skill discovery is stale")

    def test_work_inside_a_skill_folder_does_not_stale_the_registry(self) -> None:
        self.build()
        scripts = self.skills / "engineering" / "api-migration" / "scripts"
        scripts.mkdir()
        (scripts / "helper.py").write_text("print('x')\n", encoding="utf-8")
        self.assert_fresh()

    def test_a_manifest_from_an_older_router_is_repaired_by_rebuild_alone(self) -> None:
        self.build()
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("discovery_watch")
        manifest.pop("input_fingerprint_version")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_stale("Registry format is outdated")

        with mock.patch.object(registry, "refresh_runtime_snapshots") as snapshots:
            records = registry.ensure_query_registry_fresh(self.output)
        snapshots.assert_not_called()
        self.assertEqual(sorted(record["name"] for record in records), ["api-migration", "webhook-audit"])


class SelfHealTest(FreshnessFixture):
    def test_a_new_skill_is_routable_on_the_next_query_without_harness_calls(self) -> None:
        self.build()
        self.write_skill(self.skills / "engineering" / "rate-limiter", "rate-limiter", "rest rate limiting")

        with mock.patch.object(registry, "refresh_runtime_snapshots") as snapshots:
            records = registry.ensure_query_registry_fresh(self.output)

        snapshots.assert_not_called()
        self.assertIn("rate-limiter", {record["name"] for record in records})
        self.assert_fresh()

    def test_config_drift_refreshes_snapshots_within_a_budget(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8")
        self.build()
        config.write_text('[mcp_servers.context7]\ncommand = "npx"\n[mcp_servers.github]\ncommand = "g"\n')

        with mock.patch.object(registry, "refresh_runtime_snapshots") as snapshots:
            records = registry.ensure_query_registry_fresh(self.output)

        snapshots.assert_called_once_with(budget_seconds=registry.SNAPSHOT_AUTOHEAL_BUDGET_SECONDS)
        self.assertIn("github", {record["name"] for record in records if record["type"] == "mcp"})

    def test_a_slow_or_broken_harness_cli_degrades_instead_of_failing_the_query(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8")
        self.build()
        config.write_text('[mcp_servers.context7]\ncommand = "npx"\n[mcp_servers.github]\ncommand = "g"\n')

        stderr = io.StringIO()
        with (
            mock.patch.object(
                registry,
                "refresh_runtime_snapshots",
                side_effect=subprocess.TimeoutExpired("claude mcp list", 45),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            records = registry.ensure_query_registry_fresh(self.output)

        # Config-sourced capabilities are still current, and the registry is fresh.
        self.assertIn("github", {record["name"] for record in records if record["type"] == "mcp"})
        self.assert_fresh()
        self.assertIn("snapshot-runtimes", stderr.getvalue())

    def test_snapshot_budget_is_enforced_before_any_snapshot_is_written(self) -> None:
        before = {path: path.read_bytes() for path in registry.REQUIRED_SNAPSHOT_SHAPES}
        with self.assertRaises(subprocess.TimeoutExpired):
            registry.refresh_runtime_snapshots(budget_seconds=0)
        self.assertEqual(before, {path: path.read_bytes() for path in registry.REQUIRED_SNAPSHOT_SHAPES})

    def test_rebuild_inside_the_registry_lock_does_not_deadlock(self) -> None:
        errors: list[BaseException] = []

        def rebuild_while_locked() -> None:
            try:
                with registry.registry_write_lock(self.output):
                    registry.rebuild(self.output, quiet=True)
                    registry.rebuild(self.output, quiet=True)
            except BaseException as error:  # surfaced below
                errors.append(error)

        worker = threading.Thread(target=rebuild_while_locked, daemon=True)
        worker.start()
        worker.join(timeout=60)
        self.assertFalse(worker.is_alive(), "re-entrant rebuild deadlocked on the registry lock")
        self.assertEqual(errors, [])
        self.assertEqual(registry._HELD_REGISTRY_LOCKS, {})
        self.assert_fresh()


class QueryBoundaryTrustTest(FreshnessFixture):
    def _records(self) -> list[dict]:
        self.build()
        records = registry.load_registry(self.output)
        outside = self.write_skill(self.temp / "elsewhere" / "webhook-hijack", "webhook-hijack", "payment webhook")
        forged = dict(records[0], id="skill:forged", name="webhook-hijack", source_path=str(outside))
        return [*records, forged]

    def test_query_verbs_never_emit_a_source_outside_the_trusted_roots(self) -> None:
        records = self._records()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = registry.bundle(
                records, "audit payment webhook races", "claude", "", 8, self.output, verify_sources=True
            )
        names = {item["name"] for item in result["bundle"]}
        self.assertIn("webhook-audit", names)
        self.assertNotIn("webhook-hijack", names)
        self.assertIn("outside the trusted capability roots", stderr.getvalue())

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            registry.emit_search(records, "payment webhook", "claude", 10, True, self.output, verify_sources=True)
        self.assertNotIn("webhook-hijack", stdout.getvalue())

    def test_full_load_still_rejects_the_untrusted_row(self) -> None:
        records = self._records()
        (self.output / "registry.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "untrusted skill source"):
            registry.load_registry(self.output)
        self.assertEqual(len(registry.load_registry(self.output, verify_sources=False)), len(records))


if __name__ == "__main__":
    unittest.main()
