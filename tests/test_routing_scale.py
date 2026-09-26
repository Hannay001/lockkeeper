"""Routing real agent prompts: namesakes stay visible, and long prompts are routed
(clipped), not refused."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402


def skill(name: str, description: str, category: str = "software-engineering", runtimes=("claude",)) -> dict:
    return {
        "id": f"skill:{name}",
        "type": "skill",
        "name": name,
        "description": description,
        "category": category,
        "status": "active",
        "runtimes": list(runtimes),
        "source_path": f"/skills/{name}/SKILL.md",
        "registration_count": 1,
        "owner": "",
    }


def mcp(name: str, description: str) -> dict:
    return {**skill(name, description), "id": f"mcp:{name}", "type": "mcp", "source_path": ""}


RECORDS = [
    skill("cpp-memory", "Find C++ memory leaks with valgrind and sanitizers"),
    skill("node-api", "Build a node.js REST API server with express"),
    skill("dotnet-build", "Fix .NET build errors and msbuild warnings"),
    skill("kuendigung", "Kündigung schreiben: Mietvertrag und Arbeitsvertrag kündigen"),
    skill("pdf-tables", "Extract tables from scanned PDF documents with OCR"),
    skill("csv-plot", "Analyze CSV data and plot charts"),
    skill("security-audit", "Audit authentication code for vulnerabilities", "testing-security"),
    skill("scala-translate", "Translate Python code to idiomatic Scala"),
    skill("git-commit", "Write conventional git commit messages"),
    skill("ci-cd", "Set up ci-cd pipelines with GitHub Actions"),
    skill("react-perf", "Profile React component performance and re-renders"),
    skill("terraform-aws", "Provision AWS infrastructure with Terraform"),
    skill("docs-writer", "Write project documentation and READMEs"),
    skill("investor-email", "Draft investor outreach emails"),
    skill("market-research", "Check the competitive landscape and market"),
    mcp("github", "GitHub pull requests, issues and code review"),
    *(skill(f"filler-{index}", f"generic helper number {index} for data and files") for index in range(30)),
]

def ranking(records: list[dict], query: str, output: Path) -> list[tuple[str, float]]:
    return [(record["id"], score) for score, record in registry.ranked_records(records, query, "claude", output)]


class NamesakeTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="lockkeeper-namesake-")
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)

    def test_different_skills_that_share_a_name_both_rank(self) -> None:
        forms = {**skill("pdf", "Fill PDF forms and read form fields"), "id": "skill:namesake-forms"}
        tables = {**skill("pdf", "Pull tables out of PDF reports into CSV"), "id": "skill:namesake-tables"}
        ids = [rid for rid, _ in ranking([forms, tables, *RECORDS], "pdf", self.output)]
        self.assertIn("skill:namesake-forms", ids)
        self.assertIn("skill:namesake-tables", ids)

    def test_copies_of_one_skill_still_rank_once(self) -> None:
        claude_copy = {**skill("pdf-tables", "Extract tables from PDF reports"), "id": "skill:pdf:claude"}
        shared_copy = {
            **skill("PDF-Tables", "Extract  tables from PDF reports", runtimes=("shared",)),
            "id": "skill:pdf:shared",
        }
        ranked = ranking([claude_copy, shared_copy, *RECORDS], "extract tables from pdf", self.output)
        self.assertEqual(sum(rid in {"skill:pdf:claude", "skill:pdf:shared"} for rid, _ in ranked), 1)


class WordFormTest(unittest.TestCase):
    def test_plural_and_singular_find_each_other(self) -> None:
        crystal = skill("crystal-tool", "Read crystal structures from CIF files")
        dependency = skill("dep-audit", "Audit one dependency for known issues")
        for query, record in (
            ("parse the crystal structure", crystal),
            ("audit our dependencies", dependency),
            ("dependency audit", dependency),
        ):
            with self.subTest(query=query):
                self.assertGreater(registry.search_score(record, query, "claude"), 0.0)

    def test_singular_and_plural_query_terms_merge(self) -> None:
        terms = dict(registry.query_terms("structure structures dependency dependencies"))
        self.assertEqual(len(terms), 2)

    def test_short_and_symbolic_terms_match_exactly(self) -> None:
        self.assertEqual(registry.term_forms("aws"), ("aws", ("",)))
        self.assertEqual(registry.term_forms("node.js"), ("node.js", ("",)))
        self.assertFalse(registry.term_matches("aws", "awss console"))
        self.assertFalse(registry.term_matches("node.js", "node.jss runtime"))
        self.assertTrue(registry.term_matches("pdf", "merge pdfs"), "longer alphabetic terms fold plurals")

    def test_umlaut_words_match_descriptions(self) -> None:
        """Regression: the containment pre-check compared folded query terms with
        unfolded record text, so no word written with an umlaut ever matched."""
        letter = skill("brief-helper", "Kündigung schreiben für den Mietvertrag")
        other = skill("miet-helper", "Mietvertrag prüfen")
        for query in ("Kündigung Mietvertrag", "Kuendigung Mietvertrag"):
            with self.subTest(query=query):
                self.assertGreater(
                    registry.search_score(letter, query, "claude"), registry.search_score(other, query, "claude")
                )

    def test_dotted_tokens_contribute_their_parts(self) -> None:
        terms = dict(registry.query_terms("compute stats from packets.pcap into solution.py."))
        self.assertIn("pcap", terms)
        self.assertIn("py", terms)
        self.assertNotIn("solution.py.", terms)


class BodyKeywordTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="lockkeeper-body-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write_skill(self, name: str, body: str) -> dict:
        path = self.root / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: Materials helper\n---\n\n{body}\n", encoding="utf-8")
        return {**skill(name, "Materials helper"), "source_path": str(path)}

    def test_keywords_are_the_words_a_body_is_about(self) -> None:
        crystal = self.write_skill(
            "crystal", "Use pymatgen to read POSCAR files. pymatgen parses POSCAR and VASP output for the user."
        )
        others = [self.write_skill(f"other-{index}", "Read files and parse output for the user.") for index in range(5)]
        mcp_row = mcp("github", "GitHub pull requests")
        records = [crystal, *others, mcp_row]
        registry.assign_body_keywords(records)
        words = crystal["keywords"].split()
        self.assertEqual(set(words[:2]), {"pymatgen", "poscar"}, "repeated distinctive words rank first")
        self.assertIn("vasp", words)
        self.assertNotIn("the", words)
        self.assertEqual(mcp_row["keywords"], "", "only markdown capabilities have bodies")

    def test_a_word_only_the_body_mentions_routes_to_the_skill(self) -> None:
        crystal = self.write_skill("crystal", "Compute Wyckoff positions from CIF files with pymatgen.")
        plain = self.write_skill("plain", "Summarize a document.")
        registry.assign_body_keywords([crystal, plain])
        self.assertGreater(registry.search_score(crystal, "wyckoff positions", "claude"), 0.0)
        self.assertEqual(registry.search_score(plain, "wyckoff positions", "claude"), 0.0)

    def test_search_output_leaves_keywords_out(self) -> None:
        crystal = self.write_skill("crystal", "Compute Wyckoff positions with pymatgen.")
        registry.assign_body_keywords([crystal])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            registry.emit_search([crystal], "wyckoff", "claude", 5, True, self.root / "out", verify_sources=False)
        results = json.loads(buffer.getvalue())["results"]
        self.assertEqual(results[0]["name"], "crystal")
        self.assertNotIn("keywords", results[0])


class LongQueryTest(unittest.TestCase):
    def test_short_queries_are_untouched(self) -> None:
        self.assertEqual(registry.focus_query("  review   a pull request "), ("review a pull request", False))

    def test_a_long_prompt_is_clipped_to_the_term_budget_not_refused(self) -> None:
        words = [f"word{index}" for index in range(400)]
        query, clipped = registry.focus_query("Fix the flaky payment test. " + " ".join(words))
        self.assertTrue(clipped)
        self.assertTrue(query.startswith("Fix the flaky payment test."))
        distinct = {token.lower() for token in re.findall(r"[A-Za-z0-9+#.-]+", query)}
        self.assertEqual(len(distinct), registry.MAX_QUERY_TERMS)

    def test_repeated_words_do_not_count_twice(self) -> None:
        query, clipped = registry.focus_query("deploy the service " * 200)
        self.assertFalse(clipped)
        self.assertTrue(query.endswith("deploy the service"))

    def test_the_character_cap_applies(self) -> None:
        query, clipped = registry.focus_query("a" * (registry.MAX_QUERY_CHARS + 50))
        self.assertTrue(clipped)
        self.assertEqual(len(query), registry.MAX_QUERY_CHARS)

    def test_cli_parsing_accepts_a_long_prompt(self) -> None:
        args = argparse.Namespace(read_stdin=False, query=["refactor"] + [f"term{index}" for index in range(300)])
        with mock.patch.object(registry, "_warn_once") as warn:
            query = registry.query_from_args(args)
        self.assertTrue(query.startswith("refactor term0"))
        warn.assert_called_once()



if __name__ == "__main__":
    unittest.main()
