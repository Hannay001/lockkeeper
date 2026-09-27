# Changelog

All notable changes to Lockkeeper. Versions follow [semantic versioning](https://semver.org).

## Unreleased

### License

- Lockkeeper is now licensed under the [Functional Source License, Version 1.1, ALv2
  Future License](https://github.com/Hannay001/lockkeeper/blob/main/LICENSE)
  (FSL-1.1-ALv2). You can use, modify and share it for any purpose, including at work
  and on commercial projects, except offering it, or a product built from it, as a
  competing commercial product or service. Each release becomes Apache 2.0 two years
  after it ships. Releases up to and including 1.2.0 remain under the MIT license.

### New

- Telemetry asks once. `lockkeeper init` and `lockkeeper hooks install` ask whether to
  share anonymous daily usage counts (Enter means yes), only in an interactive
  terminal, never in scripts or CI or when `DO_NOT_TRACK` is set, and never again once
  answered. It stays off until you say yes. See
  [docs/TELEMETRY.md](https://github.com/Hannay001/lockkeeper/blob/main/docs/TELEMETRY.md).
- The README, the one-prompt installer and the Windows notes install from PyPI
  (`pipx install lockkeeper`), with a PyPI badge.
- Releases run from *Actions → publish → Run workflow*: it uploads to PyPI, then
  creates the tag and GitHub release with the CHANGELOG notes. See
  [docs/RELEASING.md](https://github.com/Hannay001/lockkeeper/blob/main/docs/RELEASING.md).

### Fixed

- A fresh `pip install lockkeeper` could not build its index: `rebuild` and `route`
  failed with "Required runtime snapshot is missing" until `lockkeeper
  snapshot-runtimes` was run (a git checkout hid it with seeded placeholders). They
  now capture the agent inventories themselves, or start from empty ones if the
  agents' CLIs fail. On 1.2.0, run `lockkeeper snapshot-runtimes` once first. CI now
  installs the built package into a brand-new home and routes a task.

## 1.2.0 — 2026-09-26

The biggest release since the first one: Lockkeeper picks the right skill far more
often, routes every prompt automatically in Claude Code, works inside any MCP client,
and stays fast at tens of thousands of skills.

### Highlights

- **Much better skill choices.** On the public SkillRouter benchmark (75 real agent
  tasks), a correct skill is ranked first 65.3% of the time among 26,000 skills (was
  34.7%) and 54.7% among 79,141 (was 25.3%), with no model. The set an agent receives
  now carries 52% of the skills a task needs (was 21%). See [docs/BENCHMARK.md](https://github.com/Hannay001/lockkeeper/blob/main/docs/BENCHMARK.md).
- **Route every prompt automatically.** `lockkeeper hooks install claude` adds a
  Claude Code hook that gives each prompt the installed skills that fit it, with the
  exact files to read.
- **MCP server.** `lockkeeper mcp` serves `route`, `search` and `audit` to Codex,
  Cursor, Windsurf, Cline and any other MCP client, with the index kept loaded.
- **Fast at scale.** Routing a long task over 79k skills went from 19.9 s to 2.5 s
  (26k: 6.5 s to 0.7 s).

### New

- `lockkeeper hooks install|show|remove claude [--scope user|project] [--firewall]`:
  one-command setup for per-prompt routing and, optionally, the live firewall.
- `lockkeeper route-hook`: the UserPromptSubmit hook itself. It never blocks a prompt,
  skips slash commands and short replies, and never waits on a re-index.
- `lockkeeper mcp [--runtime ...]`: MCP stdio server (protocol 2025-06-18, 2025-03-26,
  2024-11-05).
- Bundle size setting: a route returns up to 10 capabilities by default. Choose 3 to 20
  per call (`--max`, or `max` in the MCP `route` tool), with `LOCKKEEPER_BUNDLE_SIZE`,
  or with `bundle_size` in a config file. Changing it never triggers a re-index. See
  [configuration](https://github.com/Hannay001/lockkeeper/blob/main/docs/CONFIGURATION.md#bundle-size).
- `lockkeeper telemetry on|off|status|show|flush`: opt-in, anonymous daily usage
  counts. Off by default; `DO_NOT_TRACK` and CI always turn it off; no endpoint is
  configured yet. See [docs/TELEMETRY.md](https://github.com/Hannay001/lockkeeper/blob/main/docs/TELEMETRY.md).
- Body keywords: at rebuild, each skill, agent and command keeps its 24 most
  distinctive body words (tf-idf across your library), so routing matches what a
  skill covers, not only its one-line description.
- `scripts/bench_routing.py`: reproducible routing benchmark on SkillRouter Eval Core.
- `scripts/bench_route_latency.py`, run in CI with a latency budget.
- Optional decision-provider stage (Laya, hosted Jev, cross-encoders, any
  `/v1/systemone` server) with shadow mode and `scripts/eval_decision.py`.
- Resource corpora: a large reference collection routes as one capability and returns
  its best-matching entries.
- Docs: [configuration](https://github.com/Hannay001/lockkeeper/blob/main/docs/CONFIGURATION.md), [firewall](https://github.com/Hannay001/lockkeeper/blob/main/docs/FIREWALL.md),
  [benchmark](https://github.com/Hannay001/lockkeeper/blob/main/docs/BENCHMARK.md), [telemetry](https://github.com/Hannay001/lockkeeper/blob/main/docs/TELEMETRY.md),
  [releasing](https://github.com/Hannay001/lockkeeper/blob/main/docs/RELEASING.md).

### Improved

- Rare, specific words weigh more than common ones (graded IDF), and breadth of match
  counts linearly, so generic skills no longer win long tasks.
- Prompts are read the way agents write them: dotted filenames (`packets.pcap` finds
  `pcap`), plural forms, sentence punctuation, and English function words.
- Bundles fill toward the bundle size with close matches (at least half the best
  match's score) instead of stopping at four. Needed skills delivered rose from 44.7%
  to 57.7% on 26k skills and from 41.7% to 52.1% on 79k.
- Ranking is indexed, so a query only touches the skills it can match.
- The registry notices installed, removed, moved or updated skills, agents, commands
  and plugins on every query and repairs itself, ignoring harness session noise.
- The README is rewritten for new users; detailed reference moved to `docs/`.

### Fixed

- Long prompts are routed instead of rejected (the limit used to be 64 words).
- Different skills that share a name are no longer hidden behind each other.
- Words written with umlauts never matched a description.
- Decision providers could push their whole shortlist below skills they never saw, and
  low-probability providers were ignored as "flat".
- Firewall fail-open paths: unscanned script types, symlink escapes, hook JSON-escaping
  evasions, a hook crash on deeply nested JSON, LLM-pass verdict downgrades, and config
  typos that silently disabled the live hook.
- The README no longer trips Lockkeeper's own scanner.

### Upgrade notes

- The registry format changed: existing registries rebuild themselves once, on the
  first query after upgrading.
- Re-indexing reads skill bodies, so it takes longer on very large libraries (79k
  skills: 42 s to 100 s). Typical libraries still take seconds.
- Routes return up to 10 capabilities by default (typically 4 to 8 before), and the
  Claude Code hook uses the same size (it used 6). An agent that reads every routed
  skill reads about twice as much skill text. For leaner bundles, set
  `bundle_size = 6` or `LOCKKEEPER_BUNDLE_SIZE=6`.
- `search --json` results no longer include the internal `keywords` field.
- With a decision provider in `rerank` mode, the provider now only reorders its
  shortlist, using scores normalized across that shortlist.

## 1.1.2 — 2026-09-06

Security and release-integrity patch: top-level audit-target symlink traversal,
oversized and single-high hook payloads failing open, receipt verification scanning
before verifying, malformed `package.json` crashing `--check-deps`, stale package
metadata, and two install-path defects. See [ROADMAP.md](https://github.com/Hannay001/lockkeeper/blob/main/ROADMAP.md).
