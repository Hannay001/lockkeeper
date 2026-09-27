# Configuration and advanced use

Lockkeeper works out of the box. This page covers the options for when you want more
control.

## Commands at a glance

| Command | What it does |
|---|---|
| `lockkeeper rebuild` | Index every capability installed across your agents. |
| `lockkeeper route "<task>"` | Pick a small, complementary set of capabilities for a task (up to 10 by default). |
| `lockkeeper search "<keywords>"` | Rank capabilities by keywords. |
| `lockkeeper audit <path>` | Scan a skill, plugin or config for prompt injection ([details](FIREWALL.md)). |
| `lockkeeper hooks install claude` | Route every Claude Code prompt automatically. |
| `lockkeeper mcp` | Serve `route`, `search` and `audit` to any MCP client. |
| `lockkeeper doctor` | Show detected agents, index size and freshness. |
| `lockkeeper check` | Verify the index against what's on disk. |
| `lockkeeper snapshot-runtimes` | Refresh MCP, plugin and tool inventories from your agents. |
| `lockkeeper telemetry` | Opt-in anonymous usage counts; `init` and `hooks install` ask once ([details](TELEMETRY.md)). |

Add `--json` to `route`, `search` and `audit` for machine-readable output, and
`--runtime claude|codex|hermes|jcode|shared` to route from a specific agent's point of
view.

## Bundle size

A route returns up to **10** capabilities by default. The roles are filled first
(primary methods, context, integrations, execution, verification). The remaining
slots go to the next-best matches, but only while they score at least half as well as
the best match, so a narrow task still gets a short list.

Pick any size from 3 to 20. The first of these that is set wins:

| Where | Example | Applies to |
|---|---|---|
| `--max` on `route` or `route-hook`, or `max` in the MCP `route` tool | `lockkeeper route --max 6 "fix the login bug"` | that call |
| `LOCKKEEPER_BUNDLE_SIZE` environment variable | `export LOCKKEEPER_BUNDLE_SIZE=12` | your shell, or an MCP server's `env` block |
| `bundle_size` in a config file | `bundle_size = 12` in `config/local.toml`, `config/<project>.toml`, or the file named by `CAPABILITY_ROUTER_CONFIG` | every route on this machine, or in that project |

Smaller bundles use less context. Larger ones carry more of what a multi-step task
needs: on the public benchmark, going from the old behavior (about 4.5 capabilities
per route) to the default of up to 10 raised the share of needed skills delivered from
44.7% to 57.7% on 26,000 skills ([details](BENCHMARK.md#bundle-size)). Changing the
size never triggers a re-index.

## Projects and policy packs

Structural paths come from `config/default.toml`. Add per-project overlays as
`config/<name>.toml` and select them with `--project <name>`. Machine-local bindings
(created by `./install.sh` or `lockkeeper init`) live in `config/local.toml`, which is
git-ignored.

Project routing policy lives in declarative policy packs: deny lists and required
lanes. See [policies/example.json](../policies/example.json).

Runtime inventory is written to a machine-local state directory
(`~/.local/state/cap/`), never into your clone. The copies under `data/snapshots/` are
read-only seeds used before the first `snapshot-runtimes` run.

## Keeping the index fresh

You rarely need to run `rebuild` by hand. Every `route` and `search` checks the few
hundred directories that contain capability folders (well under a millisecond for 26k
skills) and a fingerprint of the settings that define capabilities: MCP servers,
plugin enablement, and Lockkeeper's own config.

- Installing, removing, moving or updating a skill, agent, command or plugin
  re-indexes on the next query.
- Session state that agents rewrite on their own (project entries, trust prompts,
  model choices) is ignored.
- Real MCP or plugin changes re-capture agent snapshots under a 45-second budget. If an
  agent's CLI is slow or broken, the query rebuilds from config and the last snapshots
  and says so on stderr.
- Editing the text of an existing `SKILL.md` is picked up by `lockkeeper check` and
  `lockkeeper rebuild`.

At rebuild, Lockkeeper keeps the 24 most distinctive words of each skill's body (by
tf-idf across your library) so routing can match what a skill covers, not just its
one-line description.

## Long prompts

Pass a whole user prompt with `--stdin`. Lockkeeper routes on its first 16,384
characters and 256 distinct terms, and notes on stderr when it clipped.

## Route every prompt automatically

```sh
lockkeeper hooks install claude            # ~/.claude/settings.json
lockkeeper hooks install claude --scope project
lockkeeper hooks show claude               # print the settings entry, change nothing
lockkeeper hooks remove claude
```

The hook (`lockkeeper route-hook`) adds a short list of fitting capabilities to each
prompt. It never blocks a prompt, skips slash commands and short replies, and never
waits on a re-index. `--format text` prints plain context for harnesses that expect
that instead of Claude Code's JSON.

## MCP server

`lockkeeper mcp [--runtime claude]` serves `route`, `search` and `audit` over MCP
(stdio). The registry stays loaded between calls and is re-validated at most every 30
seconds, so routes inside a session are fast even for large libraries.

## Semantic sidecar (optional)

The embedding sidecar in `embedder/` adds semantic re-ranking on top of lexical
scoring. It runs in its own virtual environment, and everything works without it. See
[embedder/README.md](../embedder/README.md).

## Decision providers (optional)

A decision model can judge the top 24 candidates ("would this capability help with
this task?"). Lockkeeper keeps deny rules, eligibility, required lanes and the
portfolio cap; the model can only reorder its shortlist. It's off by default and starts
in shadow mode:

```toml
# config/local.toml
[extensions.decision]
provider = "systemone"
endpoint = "http://127.0.0.1:8000/v1/systemone"   # laya-serve on this machine
mode = "shadow"                                    # "rerank" once measured
```

On the public benchmark, rerankers added 4–8 points and cost seconds per route on a
CPU. Measure on your own tasks with `scripts/eval_decision.py`. See
[decision/README.md](../decision/README.md).

## Resource corpora

A large reference collection (statutes, API pages, case law) can route as one
capability instead of thousands:

```toml
[[extensions.resource_corpora]]
name = "german-law"
root = "~/.agents/skills/german-law"
description = "German statutes and case law: BGB, HGB, GmbHG, StGB, ZPO."
```

A routed corpus lists its best-matching entries as `resources`, and
`lockkeeper search --corpus german-law "Widerruf Fernabsatz"` searches inside it.

## Windows

macOS, Linux and Windows are all covered by CI. See [windows.md](windows.md) for the
symlink-versus-junction notes.
