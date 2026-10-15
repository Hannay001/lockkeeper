"""Deployment vocabulary: per-machine names and paths that used to need code edits.

Every key lives under [extensions] in any config layer, so a deployment can name
its own entry-document suffixes, pinned skills, MCP descriptions and leaf folders
to skip without forking the router.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402
from router_config import RouterConfigError  # noqa: E402


def record(name: str, description: str = "", record_type: str = "skill", **extra) -> dict:
    return {
        "id": f"{record_type}:{name}",
        "type": record_type,
        "name": name,
        "description": description,
        "category": "software-engineering",
        "status": "active",
        "runtimes": ["claude"],
        "source_path": "",
        "registration_count": 1,
        "owner": "",
        **extra,
    }


class VocabularyFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.original = registry.ROUTER_CONFIG
        self.addCleanup(registry.configure_router, self.original)
        directory = tempfile.TemporaryDirectory(prefix="lockkeeper-vocabulary-")
        self.addCleanup(directory.cleanup)
        self.temp = Path(directory.name).resolve()

    def use(self, **extensions: object) -> None:
        registry.configure_router(replace(self.original, extensions=tuple(sorted(extensions.items()))))


class EntrypointSuffixTest(VocabularyFixture):
    def test_configured_suffixes_name_the_entry_document(self) -> None:
        plugin = self.temp / "contracts"
        plugin.mkdir()
        (plugin / "README.md").write_text("readme", encoding="utf-8")
        (plugin / "contracts-workshop.md").write_text("entry", encoding="utf-8")
        self.use(entrypoint_suffixes=["-workshop.md"])
        self.assertEqual(registry.entrypoint_document(plugin, "contracts"), plugin / "contracts-workshop.md")

    def test_without_suffixes_the_readme_is_used(self) -> None:
        plugin = self.temp / "contracts"
        plugin.mkdir()
        (plugin / "README.md").write_text("readme", encoding="utf-8")
        (plugin / "contracts-workshop.md").write_text("entry", encoding="utf-8")
        self.use()
        self.assertEqual(registry.entrypoint_document(plugin, "contracts"), plugin / "README.md")

    def test_suffixes_must_be_markdown_filenames(self) -> None:
        with self.assertRaisesRegex(RouterConfigError, "entrypoint_suffixes"):
            self.use(entrypoint_suffixes=["../escape"])


class PinnedSkillPathTest(VocabularyFixture):
    def test_pins_resolve_user_paths(self) -> None:
        self.use(pinned_skill_paths={"Voice-Pass": "~/skills/voice-pass/SKILL.md"})
        self.assertEqual(
            registry.PINNED_SKILL_PATHS,
            {"voice-pass": Path("~/skills/voice-pass/SKILL.md").expanduser()},
        )

    def test_a_pin_rejects_shadow_copies_in_exact_choices(self) -> None:
        real = self.temp / "real" / "SKILL.md"
        shadow = self.temp / "shadow" / "SKILL.md"
        self.use(pinned_skill_paths={"voice-pass": str(real)})
        records = [record("voice-pass", source_path=str(shadow))]
        self.assertIsNone(registry.exact_record(records, ["voice-pass"], {"skill"}))
        records.append(record("voice-pass", source_path=str(real), id="skill:voice-pass-real"))
        self.assertEqual(registry.exact_record(records, ["voice-pass"], {"skill"})["source_path"], str(real))

    def test_pins_must_be_a_table_of_paths(self) -> None:
        with self.assertRaisesRegex(RouterConfigError, "pinned_skill_paths"):
            self.use(pinned_skill_paths=["voice-pass"])

    def test_reconfiguring_clears_old_pins(self) -> None:
        self.use(pinned_skill_paths={"voice-pass": "/x/SKILL.md"})
        self.use()
        self.assertEqual(registry.PINNED_SKILL_PATHS, {})


class AutoDiscoveryKeepTest(VocabularyFixture):
    def test_kept_names_extend_the_router_skill(self) -> None:
        self.use(auto_discovery_keep=["graph-builder"])
        self.assertEqual(registry.AUTO_DISCOVERY_KEEP, frozenset({"capability-router", "graph-builder"}))

    def test_the_router_skill_is_always_kept(self) -> None:
        self.use()
        self.assertEqual(registry.AUTO_DISCOVERY_KEEP, frozenset({"capability-router"}))


class McpDescriptionTest(VocabularyFixture):
    def test_configured_descriptions_extend_the_built_ins(self) -> None:
        self.use(mcp_descriptions={"Notes-Server": "Team decision log."})
        self.assertEqual(registry.MCP_DESCRIPTIONS["notes-server"], "Team decision log.")
        self.assertIn("context7", registry.MCP_DESCRIPTIONS)

    def test_a_configured_description_can_replace_a_built_in(self) -> None:
        self.use(mcp_descriptions={"context7": "Docs."})
        self.assertEqual(registry.MCP_DESCRIPTIONS["context7"], "Docs.")
        self.use()
        self.assertNotEqual(registry.MCP_DESCRIPTIONS["context7"], "Docs.")

    def test_descriptions_must_be_text(self) -> None:
        with self.assertRaisesRegex(RouterConfigError, "mcp_descriptions"):
            self.use(mcp_descriptions={"x": 3})


class SkipSkillDirsTest(VocabularyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.temp / "skills"
        for leaf in ("law/tenancy/skills/notice", "law/tenancy/skills/kept-leaf", "law/deep/x/skills/other", "writer"):
            folder = self.root / leaf
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {folder.name}\n---\n", encoding="utf-8")

    def names(self) -> set[str]:
        return {path.parent.name for path in registry.iter_skill_entries(self.root)}

    def test_matching_folders_are_not_walked(self) -> None:
        self.use(skip_skill_dirs=["*/law/*/skills/*"])
        self.assertEqual(self.names(), {"writer"})

    def test_kept_names_survive_a_skip_pattern(self) -> None:
        self.use(skip_skill_dirs=["*/law/*/skills/*"], skip_skill_keep=["Kept-Leaf"])
        self.assertEqual(self.names(), {"kept-leaf", "writer"})

    def test_no_patterns_walks_everything(self) -> None:
        self.use()
        self.assertEqual(self.names(), {"notice", "kept-leaf", "other", "writer"})

    def test_patterns_must_be_strings(self) -> None:
        with self.assertRaisesRegex(RouterConfigError, "skip_skill_dirs"):
            self.use(skip_skill_dirs="*/law/*")


class RequiredMemoryMcpTest(VocabularyFixture):
    def test_a_single_name_or_a_list_is_accepted(self) -> None:
        self.use(required_memory_mcp="notes")
        self.assertEqual(registry.required_memory_mcps(), ("notes",))
        self.use(required_memory_mcp=["notes", "lessons"])
        self.assertEqual(registry.required_memory_mcps(), ("notes", "lessons"))
        self.use()
        self.assertEqual(registry.required_memory_mcps(), ())

    def test_other_shapes_are_rejected(self) -> None:
        with self.assertRaisesRegex(RouterConfigError, "required_memory_mcp"):
            self.use(required_memory_mcp=3)


class CategoryWordBoundaryTest(unittest.TestCase):
    def test_short_terms_do_not_match_inside_other_words(self) -> None:
        # "tam" (market sizing) used to match inside "datamodel" and "instamatic".
        self.assertNotEqual(
            registry.category_for("instamatic-helper", "datamodel tooling", "", "skill"),
            "market-business",
        )

    def test_short_terms_still_match_whole_words_and_plurals(self) -> None:
        self.assertEqual(registry.category_for("tam", "", "", "skill"), "market-business")
        self.assertEqual(registry.category_for("sizing", "estimate tams for a startup", "", "skill"), "market-business")


class PreferFirstTest(VocabularyFixture):
    def bundle(self, rule: dict) -> list[str]:
        records = [
            record("contracts-workshop", "draft and review commercial contracts"),
            record("legal-search", "search statutes", record_type="mcp"),
            record("web", "search the web", record_type="tool"),
        ]
        with (
            mock.patch.object(registry, "policy_pack_for", return_value={"prefer": [rule]}),
            mock.patch.object(registry, "semantic_hits", return_value={}),
            mock.patch.object(registry, "load_aliases", return_value={}),
            mock.patch.object(registry, "registry_manifest_fingerprint", return_value="fp"),
        ):
            result = registry.bundle(records, "draft a contract", "claude", "lab", 8, self.temp)
        return [item["name"] for item in result["bundle"] if item["lane"] == "integration"]

    def test_first_adds_only_the_first_choice_that_resolves(self) -> None:
        rule = {
            "match": "contract",
            "lane": "integration",
            "first": True,
            "choose": [{"mcp": "missing-server"}, {"mcp": "legal-search"}, {"tool": "web"}],
        }
        self.assertEqual(self.bundle(rule), ["legal-search"])

    def test_without_first_every_resolving_choice_is_added(self) -> None:
        rule = {"match": "contract", "lane": "integration", "choose": [{"mcp": "legal-search"}, {"tool": "web"}]}
        self.assertEqual(sorted(self.bundle(rule)), ["legal-search", "web"])


class ImportTimeVocabularyTest(unittest.TestCase):
    """Values applied while the module imports must survive the rest of the import."""

    def test_every_key_survives_import(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lockkeeper-vocab-import-") as directory:
            config = Path(directory) / "vocab.toml"
            config.write_text(
                "[extensions]\n"
                'entrypoint_suffixes = ["-guide.md"]\n'
                'auto_discovery_keep = ["graph-builder"]\n'
                'skip_skill_dirs = ["*/corpus/*"]\n'
                'skip_skill_keep = ["kept"]\n'
                "[extensions.pinned_skill_paths]\n"
                'voice-pass = "/pins/voice-pass/SKILL.md"\n'
                "[extensions.mcp_descriptions]\n"
                'notes = "Team decision log."\n',
                encoding="utf-8",
            )
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import capability_registry as r; print(r.ENTRYPOINT_SUFFIXES, sorted(r.AUTO_DISCOVERY_KEEP), "
                    "r.SKIP_SKILL_DIR_PATTERNS, sorted(r.SKIP_SKILL_KEEP), sorted(r.PINNED_SKILL_PATHS), "
                    "r.MCP_DESCRIPTIONS.get('notes'))",
                ],
                cwd=SCRIPTS_DIR,
                env={**os.environ, "CAPABILITY_ROUTER_CONFIG": str(config), "HOME": directory,
                     "LOCKKEEPER_CONFIG_DIR": str(Path(directory) / "none")},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(
            probe.stdout.strip(),
            "('-guide.md',) ['capability-router', 'graph-builder'] ('*/corpus/*',) ['kept'] ['voice-pass'] "
            "Team decision log.",
        )


if __name__ == "__main__":
    unittest.main()
