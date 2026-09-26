"""The lexical index is a pure speed-up: ranking through it must be byte-identical
to scoring every record with search_score(), and a long task must only touch the
records its terms can match.
"""
from __future__ import annotations

import random
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bench_route_latency as bench  # noqa: E402
import capability_registry as registry  # noqa: E402

# Terms a caller may pass that query_terms() never produces: unfolded umlauts,
# uppercase, spaces, symbol-only and empty. None of them can be looked up in the
# index, so they must still be scored everywhere.
ODD_TERMS = [
    ("größe", 1.0),
    ("API", 1.0),
    ("--", 1.0),
    ("c++", 0.55),
    ("x y", 1.0),
    ("", 0.25),
    ("zebra", 0.3),
]


def hand_written_records() -> list[dict]:
    """Rows for the edges of search_score(): folding, symbols, source/category-only hits."""

    def row(key: str, name: str, description: str, category: str = "specialized-other", **extra) -> dict:
        return {
            "id": f"skill:{key}",
            "type": "skill",
            "name": name,
            "description": description,
            "category": category,
            "status": "active",
            "runtimes": ["claude"],
            "source_path": f"/home/dev/.claude/skills/{key}/SKILL.md",
            "registration_count": 1,
            "owner": "",
            **extra,
        }

    return [
        # Raw umlauts: a folded query term matches them only through another field.
        row("kuendigung-raw", "Kündigung-Assistent", "Größe prüfen für Mietverträge"),
        row("kuendigung-mixed", "Kündigung-Helfer", "kuendigung letters, straße names"),
        row("kuendigung-exact", "kuendigung", "Exact-name match"),
        row("symbols", "cpp-node", "Builds c++ and node.js e-mail tools; flags like --dry-run, ## headings ..."),
        row("zebra-source", "stripes", "Unrelated words", source_path="/opt/zebra/stripes/SKILL.md"),
        row("category-only", "debugger-x", "Finds faults", category="testing-security"),
        row("uppercase", "API-Gateway", "Routes API calls"),
        row("mcp-row", "zebra-mcp", "zebra integration", type="mcp", source_path="", runtimes=["shared"]),
    ]


class LexicalIndexFixture(unittest.TestCase):
    def setUp(self) -> None:
        registry._LEXICAL_INDEXES.clear()
        self.addCleanup(registry._LEXICAL_INDEXES.clear)
        corpus = bench.SyntheticCorpus(seed=11)
        self.pool = corpus.records(400) + hand_written_records()
        self.assertGreaterEqual(len(self.pool), registry.LEXICAL_INDEX_MIN_POOL)
        rng = random.Random(5)
        self.aliases = {
            record["id"]: " | ".join(
                sorted(
                    {" ".join(corpus.sample(rng.randint(1, 3))) for _ in range(rng.randint(1, 3))}
                    | ({"größe ändern", "who else is doing this"} if index % 17 == 0 else set())
                )
            )
            for index, record in enumerate(self.pool)
            if index % 4 == 0
        }
        self.queries = [
            corpus.task(180),
            corpus.task(60),
            "Kündigungsschreiben prüfen und Größe ändern -- ## c++ node.js e-mail --dry-run",
            "kuendigung groesse aendern; who else is doing this",
            "zebra",
            "api gateway debugging and testing",
            "-- ## ... ++",
            "",
        ]
        self.queries += [intent for query in self.queries for intent in registry.query_intents(query)]

    def linear(self, pool: list[dict], query: str, terms: list, aliases: dict[str, str]) -> list[float]:
        return [
            registry.search_score(record, query, "claude", terms, aliases.get(record["id"], ""))
            for record in pool
        ]


class LexicalScoresEquivalenceTest(LexicalIndexFixture):
    def test_indexed_scores_equal_scoring_every_record(self) -> None:
        matched = 0
        for query in self.queries:
            damped = registry.damped_query_terms(registry.query_terms(query), self.pool)
            for terms in (damped, [], ODD_TERMS, ODD_TERMS[:1] + damped):
                with self.subTest(query=query[:50], terms=len(terms)):
                    expected = self.linear(self.pool, query, terms, self.aliases)
                    actual = registry.lexical_scores(self.pool, query, "claude", terms, self.aliases)
                    self.assertEqual(actual, expected)
                    matched += sum(1 for score in expected if score)
        self.assertGreater(matched, 1000, "the fixture must exercise real matches")

    def test_indexed_damping_equals_counting_every_record(self) -> None:
        for query in self.queries:
            terms = registry.query_terms(query) + ODD_TERMS
            with self.subTest(query=query[:50]):
                indexed = registry.damped_query_terms(terms, self.pool)
                with mock.patch.object(registry, "LEXICAL_INDEX_MIN_POOL", 10**9):
                    linear = registry.damped_query_terms(terms, self.pool)
                self.assertEqual(indexed, linear)

    def test_an_edited_record_is_not_scored_from_a_stale_index(self) -> None:
        query = "quokka"
        self.assertFalse(any(registry.lexical_scores(self.pool, query, "claude", None, {})))
        self.pool[3]["description"] += " Feeds a quokka."
        scores = registry.lexical_scores(self.pool, query, "claude", None, {})
        self.assertGreater(scores[3], 0.0)
        self.assertEqual(scores, self.linear(self.pool, query, [], {}))


class RankedRecordsEquivalenceTest(LexicalIndexFixture):
    def ranked(self, records: list[dict], query: str, semantic: dict[str, float]) -> list:
        output = Path("/nonexistent-lockkeeper-output")
        with (
            mock.patch.object(registry, "semantic_hits", return_value=semantic),
            mock.patch.object(registry, "registry_manifest_fingerprint", return_value="fp"),
            mock.patch.object(registry, "load_aliases", return_value=self.aliases),
        ):
            return [
                (score, record["id"], record.get("resources"))
                for score, record in registry.ranked_records(records, query, "claude", output)
            ]

    def test_ranking_with_shards_and_semantic_hits_matches_linear_scoring(self) -> None:
        corpus = bench.SyntheticCorpus(seed=23)
        parent = {
            **hand_written_records()[0],
            "id": "corpus:refs",
            "type": "corpus",
            "name": "refs",
            "description": "Reference corpus",
            "runtimes": ["shared"],
            "resource_count": 150,
            "resource_top_k": 3,
        }
        shards = [
            {**record, "id": f"shard:{index}", "parent": "corpus:refs", "rankable": False, "runtimes": ["shared"]}
            for index, record in enumerate(corpus.records(150))
        ]
        records = self.pool + [parent] + shards
        rng = random.Random(9)
        for query in self.queries:
            semantic = {record["id"]: rng.uniform(0.55, 0.85) for record in rng.sample(self.pool, 25)}
            with self.subTest(query=query[:50]):
                indexed = self.ranked(records, query, semantic)
                with mock.patch.object(registry, "LEXICAL_INDEX_MIN_POOL", 10**9):
                    linear = self.ranked(records, query, semantic)
                self.assertEqual(indexed, linear)


class LongQueryWorkTest(unittest.TestCase):
    """A deterministic stand-in for a latency budget: count the scoring work.

    Before the index, ranking a long task tested every query term against every
    record (terms x records per pass). Wall-clock limits are flaky across CI
    runners; this measures the thing that made routing slow.
    """

    def test_a_long_task_scores_only_the_records_and_terms_it_can_match(self) -> None:
        registry._LEXICAL_INDEXES.clear()
        self.addCleanup(registry._LEXICAL_INDEXES.clear)
        corpus = bench.SyntheticCorpus(seed=3)
        pool = corpus.records(4000)
        query = corpus.task(180)
        terms = registry.damped_query_terms(registry.query_terms(query), pool)
        self.assertGreater(len(terms), 60)
        with mock.patch.object(registry, "search_score", wraps=registry.search_score) as scored:
            scores = registry.lexical_scores(pool, query, "claude", terms, {})
        term_checks = sum(len(call.args[3]) for call in scored.call_args_list)
        self.assertGreater(sum(1 for score in scores if score), 0)
        self.assertLess(scored.call_count, len(pool))
        # ~5% measured on this corpus; a linear scan is 100%.
        self.assertLess(term_checks, len(pool) * len(terms) * 0.1)


class TermMatchingTest(unittest.TestCase):
    """The faster term_in_text() and fold_umlauts() keep their exact semantics."""

    @staticmethod
    def reference_term_in_text(term: str, text: str) -> bool:
        folding = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
        folded_term, folded_text = term.translate(folding), text.translate(folding)
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(folded_term)}(?![a-z0-9])", folded_text))

    def test_matches_the_lookbehind_first_pattern(self) -> None:
        rng = random.Random(1)
        alphabet = "ab1-.+#_ äöüßÄẞé\n"
        for _ in range(20000):
            term = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4)))
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
            self.assertEqual(
                registry.term_in_text(term, text), self.reference_term_in_text(term, text), (term, text)
            )
            self.assertEqual(registry.fold_umlauts(text), text.translate(registry.UMLAUT_FOLDING))

    def test_known_boundaries(self) -> None:
        self.assertTrue(registry.term_in_text("js", "node.js tools"))
        self.assertTrue(registry.term_in_text("kuendigung", "Kündigung".lower()))
        self.assertFalse(registry.term_in_text("test", "testing"))
        self.assertFalse(registry.term_in_text("c++", "abc++"))
        self.assertTrue(registry.term_in_text("--", "flags -- here"))


if __name__ == "__main__":
    unittest.main()
