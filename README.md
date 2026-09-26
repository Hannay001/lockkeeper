<h1 align="center">Lockkeeper 🔒</h1>

<p align="center">
  <b>Your AI agent doesn't need every skill you've installed. It needs the right few, checked for prompt injection.</b><br>
  Lockkeeper routes each task (or every prompt, automatically) to the handful of skills, MCP servers and tools that fit it, across Claude Code, Codex, Cursor and 7 more agents. No model, no GPU, no API key.
</p>

<p align="center">
  <a href="https://github.com/Hannay001/lockkeeper/actions/workflows/tests.yml"><img src="https://github.com/Hannay001/lockkeeper/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <img src="https://img.shields.io/badge/dependencies-0-brightgreen.svg" alt="zero dependencies">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg" alt="macOS, Linux, Windows">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#quickstart-60-seconds">Quickstart</a> ·
  <a href="#proof-a-public-benchmark">Benchmark</a> ·
  <a href="#the-firewall">Firewall</a> ·
  <a href="#how-it-compares">Comparison</a> ·
  <a href="#faq">FAQ</a>
</p>

---

|  |  |
|---|---|
| **🎯 Picks the right skill** | On a public benchmark of real agent tasks against **26,000 real `SKILL.md` files**, Lockkeeper names a correct skill first **65% of the time** (it was 35% before this release). [Reproduce it →](#proof-a-public-benchmark) |
| **📉 Keeps your prompt flat** | With **58,018 capabilities installed**, a routed task carries a median of **~8,700 tokens** of skills instead of ~77.5M. Adding skills doesn't grow your prompt. [See the numbers →](#your-prompt-stays-flat-as-your-toolbox-grows) |
| **🛡 Blocks prompt injection** | Every skill, plugin, MCP config and live tool call can be scanned before your agent sees it. Signed receipts prove what was checked. [Firewall →](#the-firewall) |
| **⚡ Fast, local, private** | Pure Python standard library. Routes in well under a second on typical libraries and runs entirely on your machine. |

## The problem

You installed skills and MCP servers to make your agent smarter. Now:

- **Your context fills up before work starts.** Three MCP servers can eat 140k tokens. Hundreds of skill descriptions crowd the prompt, and the agent picks the wrong one or none.
- **Name-and-description matching isn't enough.** Agents choose skills from one-line descriptions, so the skill that actually knows your file format or library goes unused.
- **You're running code you haven't read.** Skills copied from random repos can hide instructions that tell your agent to disregard you, or to send your keys to someone else's server, and your agent will follow them.

Lockkeeper sits between your task and your toolbox: it **indexes** everything installed across every agent on your machine, **routes** each task to a small, complementary set, and **screens** what goes in.

```console
$ lockkeeper route --runtime claude "migrate the auth module to the new token API"

status: success
summary: selected 6 complementary capabilities across 4 lanes
[context] mcp: context7
[primary] skill: api-migration
[integration] tool: mcp__context7__query_docs
[execution] tool: exec_command
[verification] agent: code-reviewer
[support] skill: python-patterns
context savings: loaded 6 of 7,540 eligible capabilities (7,534 kept out of context)
```

<p align="center">
  <img src="docs/demo-route.png" alt="lockkeeper routing a payment-webhook audit task to two primary skills, flagged as untrusted external content" width="72%">
  <br><sub><b>Route</b>: one task in, a bounded portfolio out, instead of the whole toolbox</sub>
</p>

## Quickstart (60 seconds)

**No terminal skills?** Copy the prompt in [PROMPT.md](PROMPT.md) and paste it into the AI coding agent you already use. It installs Lockkeeper, connects it to every agent it finds, and reports back in plain language.

**Terminal:**

```sh
git clone https://github.com/Hannay001/lockkeeper.git && cd lockkeeper
./install.sh                     # adds `lockkeeper` to your PATH and connects every agent it finds
lockkeeper rebuild               # index everything you have installed
lockkeeper route --runtime claude "write unit tests for a python data pipeline"
```

**pip:**

```sh
pip install git+https://github.com/Hannay001/lockkeeper.git
lockkeeper doctor
```

### Route every prompt automatically (Claude Code)

```sh
lockkeeper hooks install claude      # one command; undo with `lockkeeper hooks remove claude`
```

Every prompt you submit now reaches the agent with a short note naming the installed skills that fit it and the exact files to read, so it never has to scan your whole toolbox or remember to ask. Slash commands and short replies like "thanks" pass through untouched, and the hook never blocks a prompt. It adds a fraction of a second on typical libraries (about 2 s at 26k skills; use the MCP server for very large ones).

### Use it from inside your agent (MCP)

`lockkeeper mcp` serves `route`, `search` and `audit` to any MCP client and keeps the index loaded between calls, so your agent can ask for the right skills mid-session:

```sh
claude mcp add lockkeeper -- lockkeeper mcp --runtime claude       # Claude Code
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.lockkeeper]
command = "lockkeeper"
args = ["mcp", "--runtime", "codex"]
```

```json
// Cursor, Windsurf, Cline and other JSON-configured clients
{ "mcpServers": { "lockkeeper": { "command": "lockkeeper", "args": ["mcp"] } } }
```

Then audit anything before you install it:

```sh
lockkeeper audit ~/Downloads/some-skill --recursive --strict   # exit 2 = hostile
```

**Works with:** Claude Code · Codex · Cursor · OpenCode · Gemini CLI · GitHub Copilot · Windsurf · Cline · Jcode · Hermes, plus anything that reads `SKILL.md` or MCP configs. The installer also detects agent harnesses it doesn't know by name.

## Proof: a public benchmark

Claims about routing are cheap, so we measure. [`scripts/bench_routing.py`](scripts/bench_routing.py) downloads **SkillRouter Eval Core**, the public benchmark from the SkillRouter paper ([arXiv:2603.22455](https://arxiv.org/abs/2603.22455)): 75 real agent tasks from SkillsBench with known correct skills, hidden among real `SKILL.md` files collected from public repositories, including 780 deliberately misleading look-alikes. It routes every task through the same code path as `lockkeeper route`.

**26,000 skills, 75 tasks, one laptop CPU:**

| | Before | **Lockkeeper now** |
|---|--:|--:|
| Correct skill ranked **first** | 34.7% | **65.3%** |
| Mean reciprocal rank | 0.415 | **0.703** |
| Needed skills in the top 10 | 33.2% | **58.6%** |
| Needed skills in the routed bundle | 22.0% | **44.7%** |
| Bundle precision | 13.2% | **25.4%** |
| Route time for a ~180-word task (median, warm process) | 6.5 s | **0.7 s** |

On the **full 79,141-skill pool** a correct skill comes first **54.7%** of the time (was 25.3%), between the paper's Qwen3-Embedding-0.6B (53.3%) and Gemini embedding (56.0%) results, and roughly double the BM25 baseline (28.0%), without loading a model at all. Full tables, the model comparison and caveats: [docs/BENCHMARK.md](docs/BENCHMARK.md).

Reproduce it (downloads the ~400 MB dataset once, from a pinned revision):

```sh
python3 scripts/bench_routing.py prepare --home /tmp/lk-bench --size 26000
python3 scripts/bench_routing.py run --home /tmp/lk-bench
```

What moved the numbers, all measured on this benchmark:

- **It reads what a skill is about, not just its one-line description.** At index time, Lockkeeper keeps the 24 most distinctive words of each skill's body (the libraries, formats and domains it covers). The SkillRouter paper found that hiding the skill body costs routers 37–44 points; this recovers much of that with no model.
- **Rare words count more.** In "compute Wyckoff positions from CIF files", *wyckoff* and *cif* decide the route, not *compute* or *files*.
- **It reads prompts the way agents write them:** filenames (`packets.pcap`, `solution.py`), plurals, umlauts, sentence punctuation, and long pasted tasks.

### Your prompt stays flat as your toolbox grows

`lockkeeper route --savings` across six everyday tasks on a real machine with **58,018 eligible capabilities** (~77.5M tokens of skill bodies if you loaded them all):

| Task | Loaded | Tokens in context | Kept out |
|---|--:|--:|--:|
| migrate the auth module to a new token API | 4 | ~8,800 | 99.99% |
| audit a payment webhook for race conditions | 4 | ~7,400 | 99.99% |
| write unit tests for a python data pipeline | 4 | ~3,800 | 100.00% |
| review a react component for accessibility | 4 | ~10,400 | 99.99% |
| debug a failing CI build on github actions | 6 | ~8,700 | 99.99% |
| add rate limiting to a REST endpoint | 4 | ~11,900 | 99.98% |

An earlier run against a **5,898**-capability index gave a median of **9,164** tokens: a 10x smaller library, essentially the same prompt. Selection happens *before* the prompt, so the bundle stays in the single-digit thousands of tokens however much you install. Token counts are estimates (body bytes ÷ 4). Reproduce with `python3 scripts/bench_context_savings.py`.

## How it works

```mermaid
flowchart LR
    T["Task or prompt"] --> S["Lexical ranking<br/>names, descriptions,<br/>body keywords, rarity"]
    S --> O["Semantic re-rank<br/>(optional)"]
    O --> D["Decision model<br/>(optional)"]
    D --> P["Policy pack<br/>deny lists, required lanes"]
    P --> B["Bounded portfolio<br/>max N, one per role"]
    B --> F["Runtime filter<br/>only what this agent can run"]
    F --> R["Routed bundle"]
```

- **One index for every agent on your machine.** Skills, agents, commands, plugins, MCP servers and tools from all your harnesses, deduplicated.
- **It keeps itself fresh.** Install, remove or update a skill and the next query notices and re-indexes. `lockkeeper doctor` shows the state.
- **Bundles, not lists.** A route fills complementary lanes (primary method, context, integration, execution, verification) under a hard cap, instead of returning ten near-duplicates.
- **Only what can run.** Each agent gets only capabilities it can actually execute.
- **Optional extras, never required:** an embedding sidecar for semantic re-ranking, and a decision-model stage ([Laya](https://pypi.org/project/laya/), a cross-encoder, or any `/v1/systemone` server) that starts in shadow mode so you can measure it before trusting it. See [decision/README.md](decision/README.md).

## The firewall

<p align="center">
  <img src="docs/demo-audit.png" alt="lockkeeper audit flagging a skill as hostile for instruction override and a curl piped to sh exfiltration attempt" width="72%">
  <br><sub><b>Catch</b>: an injection attempt flagged hostile; exit codes gate installs and CI</sub>
</p>

`lockkeeper audit` is a dependency-free static scanner for skill folders, plugin manifests, MCP configs and hook payloads. It detects text that tries to override the agent's instructions, commands that send secrets or files to the network, reads of credential stores, code that decodes and runs a hidden payload, destructive commands, hidden directive comments, and invisible or look-alike Unicode. It scans Markdown, JSON/TOML/YAML and common script types. Bytecode and binaries it can't read are hashed and never audit clean, and symlinks that escape the audited folder are flagged.

- **CI-friendly verdicts:** `clean` / `suspect` / `hostile` → exit `0` / `1` / `2` under `--strict`.
- **Comparable findings:** every finding carries a SkillTrustBench taxonomy tag (T01–T09).
- **Dependency CVEs:** `--check-deps` checks pinned dependencies against osv.dev.
- **Optional LLM second pass:** `--llm-scan` (opt-in twice) adds an OpenAI-compatible review; the offline scanner never depends on it.
- **Signed receipts:** HMAC-SHA256 receipts bind to exactly what was scanned.

```sh
lockkeeper audit ~/skills/some-skill --recursive --strict --receipt-out receipt.json --receipt-key key.hex
lockkeeper audit --verify-receipt receipt.json --receipt-key key.hex   # exit 0 valid, 1 tampered
```

**Live protection:** register the firewall as a hook and hostile tool calls are blocked before they run. It fails closed on oversized input and on any high or critical finding.

```sh
lockkeeper hooks install claude --firewall    # adds a PreToolUse hook to ~/.claude/settings.json
```

Any harness with the same stdin contract can call `lockkeeper hook` directly; `lockkeeper hooks show claude --firewall` prints the exact settings entry.

<p align="center">
  <img src="docs/demo-receipt.png" alt="signed scan receipt verified intact with exit code zero" width="60%">
</p>

## How it compares

| | Routes tasks to capabilities | Covers skills, MCP, plugins, tools across agents | Uses skill bodies | Prompt-injection firewall | Needs a model / GPU |
|---|:-:|:-:|:-:|:-:|:-:|
| Loading every skill into context | ✗ | – | ✓ (at huge token cost) | ✗ | no |
| Built-in progressive disclosure (name + description) | agent guesses | per agent | ✗ | ✗ | no |
| Learned skill routers (e.g. SkillRouter, 1.2B params) | ✓ | skills only | ✓ | ✗ | yes |
| MCP server managers | ✗ | MCP only | – | ✗ | no |
| Skill security scanners | ✗ | ✗ | ✓ | ✓ | some |
| **Lockkeeper** | **✓** | **✓** | **✓ (keywords)** | **✓** | **no** |

## Configuration and advanced use

<details>
<summary><b>Projects and policy packs</b></summary>

Structural paths come from `config/default.toml`; add per-project overlays as `config/<name>.toml` and select them with `--project <name>`. Project routing policy (deny lists, required lanes) lives in declarative policy packs: see [policies/example.json](policies/example.json). Runtime inventory is written to a machine-local state dir (`~/.local/state/cap/`), never into your clone.
</details>

<details>
<summary><b>Keeping the index fresh</b></summary>

Every `route`/`search` checks the few hundred directories that contain capability folders (well under a millisecond for 26k skills) and a fingerprint of the harness settings that define capabilities: MCP servers, plugin enablement, Lockkeeper's own config. Installing, removing, moving or updating a skill, agent, command or plugin re-indexes on the next query. Session state that harnesses rewrite on their own is ignored. Real MCP/plugin changes re-capture harness snapshots under a 45-second budget; if a harness CLI is slow or broken, the query rebuilds from config and the last snapshots and says so on stderr. Editing the text of an existing `SKILL.md` is picked up by `lockkeeper check` and `rebuild`.
</details>

<details>
<summary><b>Long prompts</b></summary>

Pass a whole user prompt with `--stdin`. Lockkeeper routes on its first 16,384 characters and 256 distinct terms, and notes on stderr when it clipped.
</details>

<details>
<summary><b>Decision providers (Laya, cross-encoders)</b></summary>

A decision model can judge the top 24 candidates ("would this capability help with this task?"). Lockkeeper keeps deny rules, eligibility, required lanes and the portfolio cap; the model can only reorder its shortlist. It's off by default and starts in shadow mode:

```toml
# config/local.toml
[extensions.decision]
provider = "systemone"
endpoint = "http://127.0.0.1:8000/v1/systemone"   # laya-serve on this machine
mode = "shadow"                                    # "rerank" once measured
```

On the benchmark, rerankers added 4–8 points on top of the lexical ranking and cost seconds per route on a CPU. Measure on your own tasks with `scripts/eval_decision.py`; see [decision/README.md](decision/README.md).
</details>

<details>
<summary><b>Resource corpora</b></summary>

A large reference collection (statutes, API pages, case law) can route as one capability instead of thousands:

```toml
[[extensions.resource_corpora]]
name = "german-law"
root = "~/.agents/skills/german-law"
description = "German statutes and case law: BGB, HGB, GmbHG, StGB, ZPO."
```

A routed corpus lists its best-matching shards as `resources`, and `lockkeeper search --corpus german-law "Widerruf Fernabsatz"` searches inside it.
</details>

## FAQ

**Does Lockkeeper send my prompts or skills anywhere?**
No. Routing, indexing and auditing run locally. The only network features are opt-in: the osv.dev dependency check (`--check-deps`), the LLM scan (`--llm-scan`), remote decision providers (`allow_remote = true`), and the optional embedding sidecar, which downloads its model once.

**Is there telemetry?**
Only if you opt in. `lockkeeper telemetry on` shares anonymous daily counts (which commands ran, how fast, registry size as a bucket); never prompts, names or paths. `DO_NOT_TRACK=1` always wins. Details: [docs/TELEMETRY.md](docs/TELEMETRY.md).

**Do I need a GPU or an API key?**
No. The core is the Python standard library. The embedding sidecar and decision models are optional add-ons.

**How big a library can it handle?**
It's tested up to 79,141 skills. At typical sizes (hundreds to a few thousand capabilities) routes and re-indexing take well under a second or a few seconds.

**Will it pick worse skills than my agent would?**
Measure it: `scripts/bench_routing.py` for the public benchmark, and `scripts/eval_decision.py` for labeled tasks from your own history.

## Contributing

Issues, ideas and PRs are welcome; see [ROADMAP.md](ROADMAP.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```sh
HOME="$(mktemp -d)" python3 -m unittest discover -s tests -p "test_*.py" -t .
python3 scripts/cap_audit.py               # self-audit
python3 scripts/bench_routing.py --help    # routing benchmark
```

Found a bypass or vulnerability? Please report it privately per [SECURITY.md](SECURITY.md).

---

<p align="center">
  Built and maintained by <b><a href="https://github.com/Hannay001">Himanshu (@Hannay001)</a></b> · MIT licensed<br>
  If Lockkeeper saves you context or catches something nasty, a ⭐ helps other people find it.
</p>
