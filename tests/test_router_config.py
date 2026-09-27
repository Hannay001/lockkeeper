"""Phase A contract tests for structural router configuration.

Every path and input file in this module lives under ``TemporaryDirectory``.  The
tests deliberately exercise the public loader rather than the repository's live
configuration, so they cannot modify a developer's home directory or router
artifacts.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
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


class IsolatedRouterConfigTest(unittest.TestCase):
    """Base fixture that points config resolution and all path writes at temp roots."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._original_config = registry.ROUTER_CONFIG

    @classmethod
    def tearDownClass(cls) -> None:
        registry.configure_router(cls._original_config)

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="phase-a-router-")
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = (Path(self._temporary_directory.name) / "router").resolve(strict=False)
        self.script_path = self.root / "scripts" / "capability_registry.py"
        self.script_path.parent.mkdir(parents=True)
        self.script_path.touch()
        self.home = (Path(self._temporary_directory.name) / "home").resolve(strict=False)
        self.home.mkdir()
        self._environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                # Windows expanduser prefers USERPROFILE over HOME.
                "USERPROFILE": str(self.home),
                "CAPABILITY_ROUTER_CONFIG": "",
            },
            clear=False,
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)

    def write_config(self, relative_path: str, contents: str) -> Path:
        path = self.root / "config" / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")
        return path

    def builtins(self) -> router_config.RouterConfig:
        return router_config.load_router_config(
            script_path=self.script_path,
            include_repository=False,
            include_explicit=False,
        )

    def configure(self, **changes: object) -> router_config.RouterConfig:
        config = replace(self.builtins(), **changes)
        registry.configure_router(config)
        return config

    def test_precedence_is_builtin_then_default_then_project_then_explicit_env(self) -> None:
        builtin = self.builtins()
        self.assertEqual(builtin.output_dir, self.home / ".agents" / "capabilities")
        self.assertEqual(builtin.snapshot_dir, Path.home() / ".local" / "state" / "cap" / "snapshots")

        default_path = self.write_config(
            "default.toml",
            """
            output_dir = "default-output"

            [project]
            cwd = "default-cwd"
            """,
        )
        project_path = self.write_config(
            "phase-a.toml",
            """
            output_dir = "project-output"

            [project]
            name = "phase-a"
            cwd = "project-cwd"
            """,
        )
        explicit_path = self.root / "external" / "override.toml"
        explicit_path.parent.mkdir(parents=True)
        explicit_path.write_text(
            textwrap.dedent(
                """
                output_dir = "explicit-output"

                [project]
                cwd = "explicit-cwd"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        os.environ["CAPABILITY_ROUTER_CONFIG"] = str(explicit_path)

        config = router_config.load_router_config(project_name="phase-a", script_path=self.script_path)

        self.assertEqual(config.output_dir, explicit_path.parent / "explicit-output")
        self.assertEqual(config.cwd, explicit_path.parent / "explicit-cwd")
        self.assertEqual(config.project_name, "phase-a")
        self.assertEqual(config.active_config_paths, (default_path, project_path, explicit_path))

    def test_generic_default_project_selects_its_overlay_and_explicit_selection_overrides_it(self) -> None:
        default_path = self.write_config(
            "default.toml",
            """
            default_project = "demo"
            output_dir = "generic-output"

            [project]
            cwd = "generic-cwd"

            [extensions.future_extension.deep]
            future_setting = "accepted"
            """,
        )
        demo_path = self.write_config(
            "demo.toml",
            """
            [project]
            name = "demo"
            cwd = "demo-cwd"
            """,
        )
        alternate_path = self.write_config(
            "alternate.toml",
            """
            [project]
            name = "alternate"
            cwd = "alternate-cwd"
            """,
        )

        bare = router_config.load_router_config(script_path=self.script_path)
        explicit = router_config.load_router_config(project_name="alternate", script_path=self.script_path)

        self.assertEqual(bare.project_name, "demo")
        self.assertEqual(bare.cwd, demo_path.parent / "demo-cwd")
        self.assertEqual(bare.active_config_paths, (default_path, demo_path))
        self.assertEqual(explicit.project_name, "alternate")
        self.assertEqual(explicit.cwd, alternate_path.parent / "alternate-cwd")
        self.assertEqual(explicit.active_config_paths, (default_path, alternate_path))

    def test_extensions_namespace_is_forward_compatible_but_other_unknown_keys_are_rejected(self) -> None:
        self.write_config(
            "default.toml",
            """
            [extensions.future_extension.deep]
            enabled = true
            """,
        )
        self.assertEqual(
            router_config.load_router_config(script_path=self.script_path).catalog_path,
            Path.home() / ".local" / "state" / "cap" / "CAPABILITIES-DETAIL.md",
        )

        for contents, error in (
            ('unknown_scalar = "typo"\n', "unknown top-level structural key"),
            ('[projec]\nname = "typo"\n', "unknown top-level structural key"),
            ('[future_extension]\nenabled = true\n', "unknown top-level structural key"),
            ('[project]\nunknown_key = "typo"\n', r"unknown \[project\] key"),
            ('[hermes]\nunknown_key = "typo"\n', r"unknown \[hermes\] key"),
        ):
            with self.subTest(contents=contents):
                self.write_config("default.toml", contents)
                with self.assertRaisesRegex(router_config.RouterConfigError, error):
                    router_config.load_router_config(script_path=self.script_path)

    def test_selected_overlay_name_must_match_the_resolved_project(self) -> None:
        self.write_config("default.toml", 'default_project = "demo"\n')
        self.write_config("demo.toml", '[project]\nname = "other"\n')
        with self.assertRaisesRegex(router_config.RouterConfigError, "does not match selected project"):
            router_config.load_router_config(script_path=self.script_path)

        self.write_config("demo.toml", '[project]\nname = "demo"\n')
        explicit_path = self.root / "explicit-mismatch.toml"
        explicit_path.write_text('[project]\nname = "other"\n', encoding="utf-8")
        os.environ["CAPABILITY_ROUTER_CONFIG"] = str(explicit_path)
        with self.assertRaisesRegex(router_config.RouterConfigError, "does not match selected project"):
            router_config.load_router_config(script_path=self.script_path)

    def test_relative_config_paths_use_the_config_file_and_tilde_uses_the_temp_home(self) -> None:
        explicit_path = self.root / "settings" / "nested" / "router.toml"
        explicit_path.parent.mkdir(parents=True)
        explicit_path.write_text(
            textwrap.dedent(
                """
                output_dir = "~/output-dir"
                claude_json_path = "~/state/claude.json"

                [project]
                snapshot_dir = "snapshots"
                catalog_path = "../catalog.md"
                mcp_config_paths = ["mcp/one.json", "../mcp/two.json"]
                surface_roots = []
                cwd = "work"
                first_party_roots = ["first-party-a", "../first-party-b"]
                hermes_project_source = "hermes-source"
                skill_catalog_csv = "exports/skills.csv"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        os.environ["CAPABILITY_ROUTER_CONFIG"] = str(explicit_path)

        config = router_config.load_router_config(
            script_path=self.script_path,
            include_repository=False,
        )

        base = explicit_path.parent
        self.assertEqual(config.output_dir, self.home / "output-dir")
        self.assertEqual(config.claude_json_path, self.home / "state" / "claude.json")
        self.assertEqual(config.snapshot_dir, base / "snapshots")
        self.assertEqual(config.catalog_path, base.parent / "catalog.md")
        self.assertEqual(config.mcp_config_paths, (base / "mcp" / "one.json", base.parent / "mcp" / "two.json"))
        self.assertEqual(config.surface_roots, ())
        self.assertEqual(config.first_party_roots, (base / "first-party-a", base.parent / "first-party-b"))

    def test_missing_or_malformed_explicit_and_repository_configs_fail_closed(self) -> None:
        missing_explicit = self.root / "does-not-exist.toml"
        os.environ["CAPABILITY_ROUTER_CONFIG"] = str(missing_explicit)
        with self.assertRaisesRegex(router_config.RouterConfigError, "file does not exist"):
            router_config.load_router_config(script_path=self.script_path, include_repository=False)

        malformed_explicit = self.root / "malformed.toml"
        malformed_explicit.write_text("output_dir = [\n", encoding="utf-8")
        os.environ["CAPABILITY_ROUTER_CONFIG"] = str(malformed_explicit)
        with self.assertRaisesRegex(router_config.RouterConfigError, "could not parse TOML"):
            router_config.load_router_config(script_path=self.script_path, include_repository=False)

        os.environ["CAPABILITY_ROUTER_CONFIG"] = ""
        self.write_config("default.toml", "output_dir = [\n")
        with self.assertRaisesRegex(router_config.RouterConfigError, "could not parse TOML"):
            router_config.load_router_config(script_path=self.script_path)

    def test_missing_selected_project_config_fails_but_no_default_config_uses_builtins(self) -> None:
        self.assertEqual(
            router_config.load_router_config(script_path=self.script_path).catalog_path,
            Path.home() / ".local" / "state" / "cap" / "CAPABILITIES-DETAIL.md",
        )
        with self.assertRaisesRegex(router_config.RouterConfigError, "file does not exist"):
            router_config.load_router_config(project_name="missing", script_path=self.script_path)

    def test_project_selector_works_before_or_after_a_subcommand_and_rejects_all_duplicates(self) -> None:
        self.assertEqual(
            router_config.split_project_argument(["--project", "phase-a", "bundle", "--max", "3"]),
            ("phase-a", ["bundle", "--max", "3"]),
        )
        self.assertEqual(
            router_config.split_project_argument(["bundle", "--project=phase-a", "--max", "3"]),
            ("phase-a", ["bundle", "--max", "3"]),
        )
        for arguments in (
            ["--project", "phase-a", "bundle", "--project", "phase-a"],
            ["bundle", "--project=phase-a", "--project", "other"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(router_config.RouterConfigError, "only once"):
                    router_config.split_project_argument(arguments)

    def test_active_config_files_are_authoritative_fingerprint_inputs(self) -> None:
        self.write_config("default.toml", 'output_dir = "default"\n')
        self.write_config("phase-a.toml", '[project]\nname = "phase-a"\n')
        explicit_path = self.root / "explicit.toml"
        explicit_path.write_text('output_dir = "explicit"\n', encoding="utf-8")
        os.environ["CAPABILITY_ROUTER_CONFIG"] = str(explicit_path)
        config = router_config.load_router_config(project_name="phase-a", script_path=self.script_path)
        registry.configure_router(config)

        authoritative_paths = registry.authoritative_config_paths()
        for active_path in config.active_config_paths:
            self.assertIn(active_path, authoritative_paths)

        with mock.patch.object(registry, "authoritative_config_paths", return_value=config.active_config_paths):
            before = registry.authoritative_input_fingerprint()
            explicit_path.write_text('output_dir = "explicit-changed"\n', encoding="utf-8")
            after = registry.authoritative_input_fingerprint()
        self.assertNotEqual(before, after)

    def test_configured_claude_json_path_narrows_only_session_state(self) -> None:
        claude_json = self.root / "configured" / "claude.json"
        claude_json.parent.mkdir(parents=True)
        config = self.configure(claude_json_path=claude_json)
        self.assertEqual(registry.ROUTER_CONFIG.claude_json_path, config.claude_json_path)

        claude_json.write_text(
            '{"numStartups":1,"mcpServers":{"one":{}},"projects":{"x":{"enabledPlugins":{"p":true}}}}',
            encoding="utf-8",
        )
        before = registry.capability_relevant_bytes(claude_json)
        claude_json.write_text(
            '{"numStartups":999,"mcpServers":{"one":{}},"projects":{"x":{"enabledPlugins":{"p":true},"history":["session"]}}}',
            encoding="utf-8",
        )
        session_only_changed = registry.capability_relevant_bytes(claude_json)
        claude_json.write_text(
            '{"numStartups":999,"mcpServers":{"two":{}},"projects":{"x":{"enabledPlugins":{"p":true}}}}',
            encoding="utf-8",
        )
        capability_changed = registry.capability_relevant_bytes(claude_json)

        self.assertEqual(before, session_only_changed)
        self.assertNotEqual(before, capability_changed)
        unrelated = self.root / "other.json"
        unrelated.write_text('{"numStartups":1}', encoding="utf-8")
        self.assertEqual(registry.capability_relevant_bytes(unrelated), unrelated.read_bytes())

    def test_multiple_project_mcp_config_paths_are_discovered(self) -> None:
        first = self.root / "mcp" / "first.json"
        second = self.root / "mcp" / "second.json"
        config = self.configure(mcp_config_paths=(first, second))

        def fake_load_json(path: Path) -> dict[str, object]:
            if path == first:
                return {"mcpServers": {"phase-a-first": {}}}
            if path == second:
                return {"mcpServers": {"phase-a-second": {}}}
            return {}

        with mock.patch.object(registry, "load_toml", return_value={}), mock.patch.object(
            registry, "load_json", side_effect=fake_load_json
        ), mock.patch.object(registry, "read_prefix", return_value=""):
            sources, legacy = registry.configured_mcp_sources()

        self.assertEqual(legacy, [])
        project_sources = [row for row in sources if row["runtime"] == "project"]
        self.assertEqual(
            {(row["name"], row["source"]) for row in project_sources},
            {("phase-a-first", str(first)), ("phase-a-second", str(second))},
        )
        self.assertEqual(config.mcp_config_paths, (first, second))


class ImportTimeExtensionsTest(unittest.TestCase):
    """Extensions applied while the module imports must survive the rest of the import."""

    def test_configured_legacy_mcp_names_survive_import(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lockkeeper-legacy-") as directory:
            config = Path(directory) / "legacy.toml"
            config.write_text('[extensions]\nlegacy_mcp_names = ["Retired-Server"]\n', encoding="utf-8")
            environment = {**os.environ, "CAPABILITY_ROUTER_CONFIG": str(config), "HOME": directory}
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import capability_registry as r; print(sorted(r.LEGACY_MCP_NAMES))",
                ],
                cwd=REPOSITORY_ROOT / "scripts",
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "['retired-server']")
