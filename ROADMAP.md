# lockkeeper — Roadmap

`lockkeeper` turns the capability router from a personal inventory tool into the
**package manager and trust layer for agent capabilities**: skills, MCP
servers, plugins, tools, agents, and commands across every major coding-agent
runtime (Claude Code, Codex, Cursor, OpenCode, Hermes, Jcode).

Positioning in one line: *npm for your agent's context — with a built-in
prompt-injection firewall.*

## Why now

- Agents drown in installed capabilities; context windows fill before work starts.
- Skills/MCPs are copied from random repos with zero safety screening.
- No existing tool indexes capabilities across runtimes and routes tasks to a
  bounded portfolio (verified against the ecosystem in Aug 2026).

## v1.0 — Public foundation (current work)

- [x] De-personalized core: no hardcoded users, projects, or pinned paths.
- [x] `lockkeeper` CLI name (`capability-registry` kept as an alias).
- [x] Project policy is config-driven (any `config/<name>.toml`).
- [x] Bundle lane heuristics read optional declarative **policy packs**
      (`policies/<project>.json`) instead of hardcoded capability names.
- [x] MIT license, CI on Python 3.11–3.14, real README.
- [x] pyproject.toml packaging (flat-module wheel via `package-dir = scripts`,
      console entry points `lockkeeper` / `lockkeeper-audit` / `lockkeeper-hook`;
      CI builds the artifact and functionally smokes it, including Windows).

## v1.0.x — Platform hardening

- [x] Windows-blocking import (fcntl) made portable; per-OS venv paths,
      shutil.which CLI resolution, UTF-8 subprocess decoding.
- [x] Native Windows story: symlink privilege docs vs junction fallback,
      sys.platform-guarded integration tests, windows-latest CI job.

## v1.1 — Audit firewall (`lockkeeper audit`) — SHIPPED

Static, stdlib-only analysis of any skill/plugin/MCP definition:

- Rule families: instruction override, exfiltration (network calls touching
  secrets/env/ssh), obfuscation (base64/eval/homoglyphs/zero-width chars),
  destructive commands, hidden directive text.
- Severity-weighted verdicts: `clean` / `suspect` (warn) / `hostile` (block).
- JSON + human output; non-zero exit under `--strict` for CI and install gates.
- Optional LLM second-pass behind a provider plug-in interface (offline first).

## Competitive upgrades (from the Aug 2026 landscape scan)

Closest neighbors: Tencent AI-Infra-Guard `skill-scan` (LLM-assisted, T01-T09
taxonomy, SkillTrustBench), Cisco `mcp-scanner` (YARA+LLM engines, CVE +
binary scanning), `pipelock` (egress firewall, signed action receipts),
`parry-guard` (ML runtime-hook scanner), `SkillRouter` (body-aware retrieve-
and-rerank at ~80K skills). None combines cross-runtime routing with a
local, dependency-free install-time firewall -- that gap is lockkeeper's niche.

- [x] Taxonomy mapping: tag `lockkeeper audit` findings with T01-T09 categories so
      results are comparable with SkillTrustBench-class tools.
- [x] Non-text payloads: `.pyc`/binaries inside an audited directory get
      hashed and floored to `suspect` instead of being ignored silently.
- [x] `lockkeeper hook`: Claude Code / Codex hook mode scanning live tool payloads
      (PreToolUse-style stdin/stdout contract), extending the firewall from
      files to runtime traffic.
- [x] Optional LLM deep-scan tier: `--llm-scan` with an OpenAI-compatible
      provider via env config; off by default, failures degrade to info.
- [x] Dependency CVE gate: check skill-declared dependencies against
      osv.dev (keyless, cached) during `lockkeeper audit`.
- [x] Signed audit receipts: locally-verifiable scan reports for CI evidence.
- [x] Reranking study: body-aware second-stage ranking -- tested as a
      post-hoc rescore over live registries (14 known-answer queries,
      weights 10/25/45): Hit@1 unchanged, MRR slightly worse. Description-
      level ranking is already saturated at personal-registry scale;
      revisit only above ~50K overlapping skills or with a learned encoder.

## v1.1.2 — Security and release-integrity patch

The September 2026 repository audit found six concrete fail-open or release
integrity defects. The fixes are intentionally narrow so they can ship before
the next feature release.

- [x] Reject a symlink or Windows junction used as the top-level recursive audit
      target, preventing an audit (and opt-in LLM scan) from escaping into
      unrelated local files.
- [x] Block hook payloads that exceed the scan limit instead of allowing an
      uninspected tail.
- [x] Block any hook payload with a high/critical finding; medium-only
      `suspect` content remains warning-only to control false positives.
- [x] Make receipt verification short-circuit before scan work, so verification
      never scans or transmits the current directory unexpectedly.
- [x] Keep malformed `package.json` input from crashing `--check-deps`.
- [x] Correct package metadata to `1.1.2` and modern SPDX/setuptools fields.
- [x] Fix two install-path defects found while verifying the release:
      `snapshot-runtimes` never wrote the Codex tool snapshot on a non-seeded
      deployment (so `pip install` left every later `rebuild`/`route`
      failing), and the context-savings benchmark crashed on a partial index
      (so the README proof could not be regenerated).
- [x] Confirm the full Linux/macOS/Windows CI and wheel smoke matrix, then tag
      and publish `v1.1.2`. Shipped 2026-09-06: 16/16 jobs green on the tag,
      including the `release-version` guard, and the published artifact was
      installed from `@v1.1.2` and driven through doctor → snapshot-runtimes →
      rebuild → route plus the audit and hook firewall paths.

## v1.2 — Pre-prompt activation and measurable context budgets

The strongest launch signal was not “more skill storage.” It was that selection
must happen **before the prompt** or the catalog simply becomes another pile.

- One-command adapters for Claude Code, Codex, Cursor, Jcode, Hermes, and
  OpenCode that call `lockkeeper route` before capability bodies enter context.
- Per-runtime context budgets with explicit fallback behavior when routing
  confidence is low.
- A first-class `lockkeeper explain` view: what was eligible, selected, rejected,
  and how many body bytes/tokens each decision cost.
- Standards-compatible import adapters for the formats people already use
  (`SKILL.md`, `AGENTS.md`, MCP configs, shared Git folders), without inventing
  another hosted capability format.
- Re-run the context-savings benchmark in CI on deterministic fixtures and
  publish the artifact, so the README proof cannot silently drift.

## v1.3 — Install & lock (`lockkeeper install`, `cap.lock`)

- Sources: git URL, tarball, local path.
- Pipeline: fetch → audit gate → verify hash → place into selected runtime
  skill roots → rebuild index.
- `cap.lock` pins origin commit + content hash + audit verdict per capability.
- Runtime targets: `claude`, `codex`, `cursor`, `opencode`, `jcode`, `hermes`.

## v1.4 — Remote discovery

- `lockkeeper search <query> --remote`: GitHub API aggregator over topic-indexed skill
  repos (no central server to operate).
- Audit-on-search: remote results show cached/static verdicts before install.

## v2.x — Large-catalog optimizer

- [x] Per-route context-savings report: every `route`/`bundle` shows how many
      eligible capabilities were kept out of context; `--savings` adds an
      estimate of skill-body tokens avoided (stdlib size/heuristic, opt-in on
      the hot path). Exposed in human and JSON output under `savings`.
- Live per-capability token-cost accounting (real tokenizer, not a byte
  heuristic).
- Learned/semantic routing only where measured catalog scale and ambiguity beat
  the dependency-free lexical path.

## Hardening backlog (from the Aug 2026 deep audit)

Fixed in v1.1.x/v1.1.2: long-line scan bypass (chunked windows), env-var
interpolation exfiltration rule, credential-upload exfil rule,
suppression-marker fail-open floor, stacked hidden-text floor, hook stdin
cap, LLM-tier https-only + redacted egress, receipt-key O_EXCL|O_NOFOLLOW
creation, atomic-write mode clamp, RecursionError-safe JSON parsing, and
the mixed-type registry trust check that bricked read verbs. The September
follow-up also closed top-level audit-root symlink traversal, oversized hook
fail-open behavior, single-high hook bypasses, receipt-verification pre-scans,
malformed npm-manifest crashes, and stale package version metadata.

Deferred (documented design limits / tuning):
- Router ranking: grow alias coverage beyond ~8% of records; add a
  low-confidence floor for generic-term winners; recalibrate the semantic
  admit threshold against real queries; intent-modifier features.
- Evasion surface: paraphrased instruction overrides, multi-line command
  continuations, base64/openssl without a pipe, fullwidth-Latin homoglyphs.
- Local-trust hardening: prefer absolute harness CLI paths over PATH
  lookup; verify sidecar interpreter ownership before execution.
- Filesystem race hardening: move audit and optional LLM reads to descriptor-
  based no-follow reads where platforms support them, with identity rechecks on
  Windows.
- Release engineering: [x] tag-to-package-version gate. Next add an automated,
  attestable publish workflow with provenance.

## Non-goals

- Running a hosted registry service.
- Replacing MCP server management apps (complementary, not competing).
- Telemetry of any kind.
