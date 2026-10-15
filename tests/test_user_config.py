"""Per-user configuration directory and working-directory project selection.

An installed (pip/pipx) Lockkeeper has no writable checkout to hold config/ and
policies/, so personal settings live in a user config directory instead:
$LOCKKEEPER_CONFIG_DIR, else $XDG_CONFIG_HOME/lockkeeper, else ~/.config/lockkeeper.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402
import router_config  # noqa: E402


class UserConfigFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_config = registry.ROUTER_CONFIG

    @classmethod
    def tearDownClass(cls) -> None:
        registry.configure_router(cls._original_config)

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="lockkeeper-user-config-")
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.root = base / "router"
        self.script_path = self.root / "scripts" / "capability_registry.py"
        self.script_path.parent.mkdir(parents=True)
        self.script_path.touch()
        self.home = base / "home"
        self.home.mkdir()
        self.user_dir = base / "user-config"
        self.work = base / "work"
        self.work.mkdir()
        environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
                "CAPABILITY_ROUTER_CONFIG": "",
                "LOCKKEEPER_CONFIG_DIR": str(self.user_dir),
                "XDG_CONFIG_HOME": "",
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, path: Path, contents: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")
        return path

    def user(self, relative: str, contents: str) -> Path:
        return self.write(self.user_dir / relative, contents)

    def repo(self, relative: str, contents: str) -> Path:
        return self.write(self.root / "config" / relative, contents)

    def load(self, project: str = "", cwd: Path | None = None) -> router_config.RouterConfig:
        return router_config.load_router_config(
            project_name=project, script_path=self.script_path, cwd=cwd or self.work
        )


class UserConfigDirectoryTest(UserConfigFixture):
    def test_the_environment_variable_names_the_directory(self) -> None:
        self.assertEqual(router_config.user_config_dir(), self.user_dir)

    def test_xdg_config_home_is_the_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"LOCKKEEPER_CONFIG_DIR": "", "XDG_CONFIG_HOME": str(self.home / "xdg")}):
            self.assertEqual(router_config.user_config_dir(), self.home / "xdg" / "lockkeeper")

    def test_dot_config_is_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"LOCKKEEPER_CONFIG_DIR": "", "XDG_CONFIG_HOME": ""}):
            self.assertEqual(router_config.user_config_dir(), self.home / ".config" / "lockkeeper")

    def test_user_config_overrides_repository_local_bindings(self) -> None:
        self.repo("local.toml", "bundle_size = 6\n")
        user_file = self.user("config.toml", "bundle_size = 7\n")
        config = self.load()
        self.assertEqual(config.bundle_size, 7)
        self.assertIn(user_file, config.active_config_paths)

    def test_user_relative_paths_resolve_against_the_user_directory(self) -> None:
        self.user("config.toml", 'output_dir = "state/capabilities"\n')
        self.assertEqual(self.load().output_dir, self.user_dir / "state" / "capabilities")

    def test_a_missing_user_directory_changes_nothing(self) -> None:
        self.repo("default.toml", "bundle_size = 5\n")
        self.assertFalse(self.user_dir.exists())
        self.assertEqual(self.load().bundle_size, 5)


class UserProjectOverlayTest(UserConfigFixture):
    def test_a_user_project_overlay_can_be_selected(self) -> None:
        # Absolute temp paths: on Windows a drive-less "/lab" resolves against the
        # config file's drive, which need not be the test runner's.
        lab = self.work / "lab"
        self.user("projects/lab.toml", f'[project]\nname = "lab"\ncwd = "{lab.as_posix()}"\n')
        config = self.load(project="lab")
        self.assertEqual(config.project_name, "lab")
        self.assertEqual(config.cwd, lab)

    def test_a_user_project_shadows_a_repository_project_of_the_same_name(self) -> None:
        repo_lab, user_lab = self.work / "repo-lab", self.work / "user-lab"
        self.repo("lab.toml", f'[project]\nname = "lab"\ncwd = "{repo_lab.as_posix()}"\n')
        self.user("projects/lab.toml", f'[project]\nname = "lab"\ncwd = "{user_lab.as_posix()}"\n')
        self.assertEqual(self.load(project="lab").cwd, user_lab)

    def test_available_projects_lists_both_locations(self) -> None:
        self.repo("shared.toml", '[project]\nname = "shared"\n')
        self.user("projects/lab.toml", '[project]\nname = "lab"\n')
        with mock.patch.object(registry, "_router_root", return_value=self.root):
            self.assertEqual(registry.available_projects(), ["lab", "shared"])

    def test_user_policy_packs_take_precedence(self) -> None:
        (self.root / "policies").mkdir(parents=True)
        (self.root / "policies" / "lab.json").write_text(json.dumps({"enable_routes": ["harness"]}), encoding="utf-8")
        self.user("policies/lab.json", json.dumps({"enable_routes": ["software"]}))
        with mock.patch.object(registry, "_router_root", return_value=self.root):
            self.assertEqual(registry.policy_pack_for("lab")["enable_routes"], ["software"])

    def test_user_config_files_are_part_of_the_freshness_fingerprint(self) -> None:
        user_file = self.user("config.toml", "bundle_size = 7\n")
        project_file = self.user("projects/lab.toml", '[project]\nname = "lab"\n')
        registry.configure_router(self.load())
        paths = set(registry.authoritative_config_paths())
        self.assertIn(user_file, paths)
        self.assertIn(project_file, paths)


class ProjectRootsTest(UserConfigFixture):
    def setUp(self) -> None:
        super().setUp()
        self.lab = self.work / "lab"
        self.nested = self.lab / "notebooks"
        self.nested.mkdir(parents=True)
        self.user("projects/lab.toml", '[project]\nname = "lab"\n')
        self.user("projects/notebooks.toml", '[project]\nname = "notebooks"\n')
        self.user(
            "config.toml",
            f"""
            [project_roots]
            lab = ["{self.lab.as_posix()}"]
            notebooks = "{self.nested.as_posix()}"
            """,
        )

    def test_working_inside_a_root_selects_its_project(self) -> None:
        self.assertEqual(self.load(cwd=self.lab / "src").project_name, "lab")

    def test_the_deepest_matching_root_wins(self) -> None:
        self.assertEqual(self.load(cwd=self.nested / "deep").project_name, "notebooks")

    def test_outside_every_root_falls_back_to_the_default_project(self) -> None:
        self.assertEqual(self.load(cwd=self.work).project_name, "")

    def test_an_explicit_project_beats_the_working_directory(self) -> None:
        self.assertEqual(self.load(project="notebooks", cwd=self.lab).project_name, "notebooks")

    def test_a_root_must_name_a_configured_project(self) -> None:
        self.user("config.toml", f'[project_roots]\nghost = "{self.lab.as_posix()}"\n')
        with self.assertRaisesRegex(router_config.RouterConfigError, "ghost"):
            self.load(cwd=self.lab)

    def test_project_roots_must_be_paths(self) -> None:
        self.user("config.toml", "[project_roots]\nlab = 3\n")
        with self.assertRaisesRegex(router_config.RouterConfigError, "project_roots.lab"):
            self.load(cwd=self.lab)


class EffectiveProjectTest(UserConfigFixture):
    def test_the_routed_project_follows_the_resolved_config(self) -> None:
        self.user("projects/lab.toml", '[project]\nname = "lab"\n')
        self.user("config.toml", f'[project_roots]\nlab = "{self.work.as_posix()}"\n')
        registry.configure_router(self.load(cwd=self.work))
        self.assertEqual(registry.effective_project(""), "lab")
        self.assertEqual(registry.effective_project("Other"), "other")


if __name__ == "__main__":
    unittest.main()
