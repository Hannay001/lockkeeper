"""Library mode: move skills out of the folder an agent loads on its own, keep them
routable, and put them back on request. Nothing moves without --apply."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402
import library  # noqa: E402
import route_hook  # noqa: E402

from tests.test_registry_freshness import FreshnessFixture  # noqa: E402


class LibraryTest(FreshnessFixture):
    def setUp(self) -> None:
        super().setUp()
        self.claude_skills = self.home / ".claude" / "skills"
        for name, description in (
            ("pdf-tables", "Extract tables from scanned PDF documents"),
            ("stripe-webhooks", "Verify Stripe webhook signatures and retries"),
            ("git-commits", "Write conventional git commit messages"),
            ("capability-router", "Route a task to the right installed capabilities"),
        ):
            self.write_skill(self.claude_skills / name, name, description)
        self.library = self.home / ".agents" / "library" / "claude"

    def library_cli(self, *argv: str) -> tuple[int, str]:
        parser = argparse.ArgumentParser()
        library.add_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["library", *argv])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = library.run(args, lambda: registry.rebuild(self.output, quiet=True))
        return code, buffer.getvalue()

    def install_hook(self) -> None:
        route_hook.install(self.home / ".claude" / "settings.json", firewall=False)

    def names_in(self, folder: Path) -> set[str]:
        return {child.name for child in folder.iterdir()} if folder.is_dir() else set()

    def test_a_plan_changes_nothing(self) -> None:
        code, shown = self.library_cli("move")
        self.assertEqual(code, 0)
        self.assertIn("would move 3", shown)
        self.assertIn("fewer tokens in every session", shown)
        self.assertIn("keeping: capability-router", shown)
        self.assertEqual(len(self.names_in(self.claude_skills)), 4)
        self.assertFalse(self.library.exists())

    def test_it_refuses_to_move_skills_the_agent_could_no_longer_reach(self) -> None:
        code, shown = self.library_cli("move", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("lockkeeper hooks install claude", shown)
        self.assertEqual(len(self.names_in(self.claude_skills)), 4)

    def test_moved_skills_leave_the_agent_folder_but_still_route(self) -> None:
        self.install_hook()
        code, shown = self.library_cli("move", "--apply", "--keep", "git-commits")
        self.assertEqual(code, 0, shown)
        self.assertEqual(self.names_in(self.claude_skills), {"capability-router", "git-commits"})
        self.assertEqual(self.names_in(self.library), {"pdf-tables", "stripe-webhooks"})
        manifest = json.loads(library.manifest_path().read_text(encoding="utf-8"))
        self.assertEqual(sorted(move["name"] for move in manifest["moves"]), ["pdf-tables", "stripe-webhooks"])

        records = registry.assert_registry_fresh(self.output, deep=False)
        result = registry.bundle(records, "verify stripe webhook signatures", "claude", "", 10, self.output)
        routed = {item["name"]: item["load_path"] for item in result["bundle"]}
        self.assertIn("stripe-webhooks", routed)
        self.assertTrue(Path(routed["stripe-webhooks"]).is_relative_to(self.library), routed)

    def test_restore_puts_skills_back_and_forgets_them(self) -> None:
        self.install_hook()
        self.library_cli("move", "--apply")
        code, shown = self.library_cli("restore")
        self.assertIn("would restore 3", shown)
        self.assertEqual(self.names_in(self.claude_skills), {"capability-router"}, "a plan changes nothing")

        code, shown = self.library_cli("restore", "--apply")
        self.assertEqual(code, 0, shown)
        self.assertEqual(len(self.names_in(self.claude_skills)), 4)
        self.assertEqual(json.loads(library.manifest_path().read_text(encoding="utf-8"))["moves"], [])
        records = registry.assert_registry_fresh(self.output, deep=False)
        paths = {row["name"]: row["source_path"] for row in records if row["type"] == "skill"}
        self.assertTrue(Path(paths["pdf-tables"]).is_relative_to(self.claude_skills))

    def test_restore_never_overwrites_a_skill_installed_again_meanwhile(self) -> None:
        self.install_hook()
        self.library_cli("move", "--apply")
        self.write_skill(self.claude_skills / "pdf-tables", "pdf-tables", "a newer copy")
        code, shown = self.library_cli("restore", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("kept in the library: pdf-tables", shown)
        self.assertIn("a newer copy", (self.claude_skills / "pdf-tables" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((self.library / "pdf-tables").is_dir())
        remaining = json.loads(library.manifest_path().read_text(encoding="utf-8"))["moves"]
        self.assertEqual([move["name"] for move in remaining], ["pdf-tables"])

    def test_restore_can_pick_skills_by_name(self) -> None:
        self.install_hook()
        self.library_cli("move", "--apply")
        self.library_cli("restore", "git-commits", "--apply")
        self.assertEqual(self.names_in(self.claude_skills), {"capability-router", "git-commits"})

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_a_linked_skill_moves_as_a_link(self) -> None:
        source = self.home / "my-skills" / "linked-skill"
        self.write_skill(source, "linked-skill", "A skill kept in its own repository")
        try:
            (self.claude_skills / "linked-skill").symlink_to(source, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not permitted here")
        self.install_hook()
        self.library_cli("move", "--apply")
        moved = self.library / "linked-skill"
        self.assertTrue(moved.is_symlink())
        self.assertEqual(moved.resolve(), source.resolve())
        self.assertTrue((source / "SKILL.md").is_file(), "the link's target is never touched")

    def test_the_claude_code_plugin_counts_as_routing(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"enabledPlugins": {"lockkeeper@lockkeeper": True}}), encoding="utf-8")
        code, shown = self.library_cli("move", "--apply")
        self.assertEqual(code, 0, shown)
        self.assertEqual(self.names_in(self.claude_skills), {"capability-router"})

    def test_status_counts_what_each_agent_loads(self) -> None:
        code, shown = self.library_cli("status")
        self.assertEqual(code, 0)
        self.assertRegex(shown, r"claude\s+4 loaded \(about \d+ tokens per session\)")
        self.assertIn("routing for claude: not set up", shown)

    def test_other_agents_need_force(self) -> None:
        self.write_skill(self.home / ".codex" / "skills" / "one", "one", "a codex skill")
        code, shown = self.library_cli("move", "--agent", "codex", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("--force", shown)
        code, shown = self.library_cli("move", "--agent", "codex", "--apply", "--force")
        self.assertEqual(code, 0, shown)
        self.assertTrue((self.home / ".agents" / "library" / "codex" / "one" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
