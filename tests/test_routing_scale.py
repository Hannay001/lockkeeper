"""Routing real agent prompts: long prompts are routed (clipped), not refused."""
from __future__ import annotations

import argparse
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402


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
