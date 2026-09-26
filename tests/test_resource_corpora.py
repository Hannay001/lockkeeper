"""Hierarchical routing: a large corpus of shard skills routes as one capability.

Shards leave global ranking and the semantic index; their lexical hits lift the
parent, and a selected parent carries its best shards as resources.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
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

from tests.test_registry_freshness import FreshnessFixture  # noqa: E402

LAW_TERMS = [
    "Widerrufsrecht Fernabsatzvertrag Verbraucher BGB 312g",
    "Kuendigung Mietrecht Wohnraum BGB 573",
    "Gesellschaftsvertrag GmbH Stammkapital GmbHG",
    "Arbeitsvertrag Probezeit Kuendigungsfrist BGB 622",
]


class CorpusFixture(FreshnessFixture):
    def setUp(self) -> None:
        super().setUp()
        self.law = self.skills / "german-law"
        for index in range(40):
            self.write_skill(
                self.law / f"band-{index // 10}" / f"shard-{index:02d}",
                f"de-law-{index:02d}",
                f"Paragraph {index} {LAW_TERMS[index % len(LAW_TERMS)]}",
            )
        self.use_corpora([{"name": "german-law", "root": str(self.law), "description": "German statutes and case law."}])

    def use_corpora(self, corpora: list[dict]) -> None:
        registry.configure_router(
            replace(registry.ROUTER_CONFIG, extensions=(("resource_corpora", corpora),))
        )

    def route(self, query: str, pack: dict | None = None) -> dict:
        with (
            mock.patch.object(registry, "semantic_hits", return_value={}),
            mock.patch.object(registry, "policy_pack_for", return_value=pack or {}),
        ):
            return registry.bundle(
                registry.load_registry(self.output), query, "claude", "", 8, self.output, verify_sources=True
            )


class CorpusRegistryTest(CorpusFixture):
    def test_shards_become_children_of_one_corpus_capability(self) -> None:
        self.build()
        records = registry.load_registry(self.output)
        corpus = [record for record in records if record["type"] == "corpus"]
        shards = [record for record in records if record.get("parent")]
        self.assertEqual([record["id"] for record in corpus], ["corpus:german-law"])
        self.assertEqual(corpus[0]["resource_count"], 40)
        self.assertEqual(len(shards), 40)
        self.assertTrue(all(record["rankable"] is False for record in shards))
        self.assertTrue(all(record.get("parent") is None for record in records if "german-law" not in record["source_path"]))
        with contextlib.redirect_stdout(io.StringIO()):
            registry.run_check(self.output, require_links=False)

    def test_a_corpus_entry_document_is_the_parent(self) -> None:
        self.write_skill(self.law, "german-law-db", "Search German federal law by statute and section.")
        self.build()
        records = {record["id"]: record for record in registry.load_registry(self.output)}
        parent = next(record for record in records.values() if record.get("resource_count"))
        self.assertEqual(parent["name"], "german-law-db")
        self.assertEqual(parent["type"], "skill")
        self.assertNotIn("corpus:german-law", records)
        self.assertTrue(all(records[r]["parent"] == parent["id"] for r in records if records[r].get("parent")))

    def test_shards_are_left_out_of_the_semantic_index(self) -> None:
        spec = importlib.util.spec_from_file_location("embed_under_test", REPOSITORY_ROOT / "embedder" / "embed.py")
        embed = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(embed)
        self.build()
        records = registry.load_registry(self.output)
        embedded = [record for record in records if not embed._is_non_rankable(record)]
        self.assertEqual(len(embedded), len(records) - 40)


class CorpusRoutingTest(CorpusFixture):
    def test_a_legal_task_routes_the_corpus_with_its_best_shards(self) -> None:
        self.build()
        result = self.route("Widerrufsrecht beim Fernabsatzvertrag nach BGB 312g")
        corpus = next(item for item in result["bundle"] if item["name"] == "german-law")
        self.assertEqual(corpus["lane"], "primary")
        self.assertEqual(len(corpus["resources"]), registry.RESOURCE_TOP_K)
        self.assertTrue(all("312g" in Path(r["load_path"]).read_text(encoding="utf-8") for r in corpus["resources"]))
        self.assertNotIn("de-law", " ".join(item["name"] for item in result["bundle"]))
        savings = result["savings"]
        self.assertEqual(savings["resource_shards"], {"indexed": 40, "selected": 5, "avoided": 35})
        self.assertEqual(savings["eligible_capabilities"], 3, "two skills + the corpus; shards are counted apart")

    def test_an_unrelated_task_does_not_surface_shards(self) -> None:
        self.build()
        result = self.route("migrate the api tokens")
        self.assertEqual([item["name"] for item in result["bundle"] if item["lane"] == "primary"], ["api-migration"])
        self.assertFalse(any(item.get("resources") for item in result["bundle"]))

    def test_denying_the_corpus_hides_its_shards_too(self) -> None:
        self.build()
        result = self.route("Kuendigung Mietrecht Wohnraum", pack={"deny": [{"types": ["corpus"]}]})
        self.assertEqual(result["bundle"], [])

    def test_search_inside_a_corpus(self) -> None:
        self.build()
        records = registry.load_registry(self.output)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            registry.emit_corpus_search(records, "german-law", "Kuendigung Mietrecht", "claude", 3, True, True)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(len(payload["results"]), 3)
        self.assertTrue(all("573" in item["description"] for item in payload["results"]))
        with self.assertRaisesRegex(RuntimeError, "no resource corpus named"):
            registry.emit_corpus_search(records, "nope", "x", "claude", 3, True, True)


class CorpusConfigTest(unittest.TestCase):
    def test_invalid_corpus_declarations_fail_loudly(self) -> None:
        for raw in (
            "german-law",
            [{"root": "/x"}],
            [{"name": "Bad Name", "root": "/x"}],
            [{"name": "a", "root": ""}],
            [{"name": "a", "root": "/x", "top_k": 0}],
            [{"name": "a", "root": "/x", "extra": 1}],
            [{"name": "a", "root": "/x"}, {"name": "a", "root": "/y"}],
        ):
            with self.subTest(raw=raw), self.assertRaises(RouterConfigError):
                registry.parse_resource_corpora(raw)

    def test_extension_typos_are_config_errors_not_import_crashes(self) -> None:
        """A bare RuntimeError at import disabled every command, the live hook included."""
        original = registry.ROUTER_CONFIG
        self.addCleanup(registry.configure_router, original)
        for extensions in (
            (("extra_skill_roots", "oops"),),
            (("legacy_mcp_names", [1]),),
            (("resource_corpora", {"name": "x"}),),
        ):
            with self.subTest(extensions=extensions), self.assertRaises(RouterConfigError):
                registry.configure_router(replace(original, extensions=extensions))


class CommonTermQueryTest(unittest.TestCase):
    def pool(self, count: int = 40) -> list[dict]:
        return [
            {"name": f"python-tool-{index}", "description": "python tests for data pipelines"}
            for index in range(count)
        ]

    def test_a_query_of_only_common_terms_is_not_neutralised(self) -> None:
        terms = registry.query_terms("python tests")
        self.assertEqual(registry.damped_query_terms(terms, self.pool()), terms)

    def test_common_terms_are_still_damped_next_to_a_rare_one(self) -> None:
        pool = [*self.pool(), {"name": "pytest-fixtures", "description": "fixture factories"}]
        damped = dict(registry.damped_query_terms(registry.query_terms("python fixture"), pool))
        self.assertLess(damped["python"], 1.0)
        self.assertGreaterEqual(damped["fixture"], 1.0)

    def test_rarer_terms_weigh_more(self) -> None:
        pool = [
            *self.pool(200),
            *({"name": f"csv-{index}", "description": "csv reports"} for index in range(8)),
            {"name": "wyckoff", "description": "wyckoff positions of crystal structures"},
        ]
        damped = dict(registry.damped_query_terms(registry.query_terms("wyckoff csv python"), pool))
        self.assertGreater(damped["wyckoff"], damped["csv"])
        self.assertGreater(damped["csv"], damped["python"])

    def test_soft_terms_are_never_boosted(self) -> None:
        pool = [*self.pool(), {"name": "summarizer", "description": "summarize meeting notes"}]
        damped = dict(registry.damped_query_terms(registry.query_terms("summarize notes"), pool))
        self.assertLessEqual(damped["summarize"], registry.SOFT_TERM_WEIGHT)


if __name__ == "__main__":
    unittest.main()
