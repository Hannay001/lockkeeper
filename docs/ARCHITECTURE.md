# Architecture

## Current engine (v0.x, single module)

`scripts/capability_registry.py` is a deliberate standard-library-only
monolith with these internal seams:

```
config (router_config.py)
  └── discovery ──► registry ──► search/bundle ──► emit
   skills, plugins,   jsonl shards,  lexical score,
   mcps, tools        manifest +    optional semantic
   (runtime snaps)    fingerprint   sidecar, lane select
```

- **Discovery** walks skill roots and plugin caches per runtime, parses
  SKILL.md frontmatter, imports MCP/plugin/tool snapshots.
- **Registry** writes `registry.jsonl`, `registrations.jsonl`, category
  shards, and a manifest with a content fingerprint, a capability-scoped
  config fingerprint, and a discovery watch. Queries self-heal staleness once
  (see Freshness model).
- **Search** is lexical scoring with alias/intent damping; an embedder
  sidecar adds cosine re-ranking when available. Pools of 128+ rows are scored
  through an in-memory token index, built once per process and reused by a
  route's intent passes. The index only scores the records and terms a query
  can match, so a long task no longer costs terms x records, and every score is
  identical to scoring each record in turn. `scripts/bench_route_latency.py`
  times routes on a large synthetic registry.
- **Decision stage** (optional, `scripts/decision_provider.py`): a provider
  judges the top of the ranking and its probabilities are blended in. It never
  decides eligibility, deny rules, lanes, or the portfolio cap.
- **Bundle** selects a bounded portfolio across lanes: context, primary,
  integration, execution, verification, output. Resource corpora route as one
  capability that carries its best shards as `resources`.

## Freshness model

A query serves the registry only when all of these hold; otherwise it repairs
itself once under the per-output lock (`rebuild` and `reindex` take the same
lock) and re-checks:

| Check | Cost | Repair |
|---|---|---|
| Runtime snapshots are not newer than `registry.jsonl` | 5 stats | rebuild |
| `registry.jsonl` matches the manifest fingerprint | parse | rebuild |
| Capability-scoped config fingerprint is unchanged | ~10 small files | snapshots (45s budget, degrades) + rebuild |
| Discovery watch: containers of capability folders are unchanged | a few hundred stats | rebuild |
| Manifest was written by this router version | none | rebuild |

The config fingerprint hashes only what discovery reads: `mcpServers` and
plugin keys from Claude settings, only the router cwd's project from
`~/.claude.json`, and `mcp_servers`/`plugins` from Codex's `config.toml`. Harness
session state doesn't count. The discovery watch records the mtime of every
directory that contains a skill, agent, command, or plugin folder; creating,
removing, or renaming an entry changes its parent's mtime. A directory that
changed during the rebuild's own walk is recorded as changed, so the next
query rediscovers. Edits inside an existing skill folder are left to
`check`/`rebuild`, which walk every root and verify every row's source against
the trusted roots. Query verbs check source trust only on the rows they emit.

## v1 upgrade seams

1. **Config-driven projects.** Any `config/<name>.toml` is a valid project;
   the CLI validates against files on disk instead of a hardcoded allowlist.
2. **Policy packs** (`policies/<project>.json`, selected with `--project`):

```json
{
  "deny": [{"types": ["mcp"], "names": ["some-server"]}],
  "require_context": [
    {"choose": [{"tool": "mcp__myproject__search_decisions"}, {"mcp": "myproject"}],
     "why": "Load prior decisions before acting.", "required": false}
  ],
  "prefer": [{"match": "\\b(library|sdk)\\b", "choose": [{"mcp": "context7"}],
              "lane": "context", "why": "Prefer live docs."}],
  "enable_routes": ["harness", "browser", "software"],
  "output_lane": {"choose": [{"skill": "your-output-polisher"}], "why": "..."}
}
```

- `deny` is absolute: denied capabilities never enter bundles, even for
  required lanes. Rule shapes are validated and fail loudly.
- `require_context` resolves each choice with its own type, in order.
- `prefer` adds matching capabilities to a lane with a score boost.
- `enable_routes` opts into the built-in demo routes (off by default).
- `output_lane` fires only on external-facing task keywords.

3. **Audit subsystem** (`scripts/cap_audit.py`): pure functions over file bytes
   → findings → verdict (`clean`/`suspect`/`hostile`). No network, no model by
   default. Used standalone (`lockkeeper audit`) and as the gate for future installs.
   Suppression markers are always surfaced as findings themselves.
4. **Installer** (planned, see ROADMAP): fetch → audit → hash-pin → place →
   rebuild. Not implemented in this version; `lockkeeper install` does not exist yet.
5. **Packaging**: install.sh symlinks `lockkeeper` (plus the legacy `cap`
   and `capability-registry` aliases). The wheel ships the flat modules plus the
   `lockkeeper_embedder` and `lockkeeper_decision` sidecar packages.
6. **Decision providers** (`decision/`): `/v1/systemone` over HTTP (laya-serve,
   hosted Jev), or a sidecar venv running Laya, a fastembed cross-encoder, or a
   token-overlap baseline. The default is off; once configured it starts in
   shadow mode, and every failure falls back to unchanged routing.
7. **Resource corpora** (`[[extensions.resource_corpora]]`): shards under a
   corpus root get `parent` and `rankable: false`. They leave global ranking and
   the semantic index, and their lexical hits roll up into the parent.

## Invariants kept from v0.x

- Standard library only in the core path; semantic and decision sidecars stay optional.
- No decision provider configured means byte-identical routing.
- Invalid startup configuration blocks public operations instead of falling back.
- Generated catalogs never load wholesale; routing returns bounded portfolios.
- Descriptions and registry metadata are treated as untrusted input everywhere.
