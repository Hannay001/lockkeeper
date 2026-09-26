# Routing benchmark

How well does Lockkeeper pick the right capabilities for a task, and how fast? This
page reports results on a public benchmark, how to reproduce them, and what they
do and don't show.

## The benchmark

**SkillRouter Eval Core** ([dataset](https://huggingface.co/datasets/pipizhao/SkillRouter-Eval-Core),
from the SkillRouter paper, [arXiv:2603.22455](https://arxiv.org/abs/2603.22455)):

- **Tasks:** 87 real agent tasks from SkillsBench. Following the paper's protocol, the
  12 tasks marked `generic_only` are excluded, leaving 75 (16 need one skill, 59 need
  two to six). Task texts are long: a median of ~170 words.
- **Skill pool:** real `SKILL.md` files collected from public repositories. The
  "hard" tier (79,141 skills) adds 780 LLM-written look-alikes of the correct skills.
- **Labels:** the correct skills for each task, matched **by skill id**. A different
  skill with the same name or an identical copy counts as a miss.

[`scripts/bench_routing.py`](../scripts/bench_routing.py) downloads a pinned revision,
writes the skills as real skill folders into an isolated home directory, runs
`lockkeeper rebuild` there, and routes every task through the same code path as
`lockkeeper route` (lexical ranking only: no embedding sidecar, no decision model).

| Metric | Meaning |
|---|---|
| Hit@1 | The top-ranked capability is a correct skill. |
| MRR | Mean of 1/rank of the first correct skill, over the full ranking. |
| Recall@K | Share of a task's correct skills in the top K. |
| Bundle recall / precision | Share of correct skills in the routed bundle (default size, up to 10), and share of the bundle that is correct: what the agent actually receives. |

## Results

Before = the router at the start of this work; after = this release. Measured on a
4-core cloud VM, CPU only.

### 79,141 skills (full hard tier)

| | Before | After |
|---|--:|--:|
| Hit@1 | 0.253 | **0.547** |
| MRR | 0.318 | **0.628** |
| nDCG@10 | 0.244 | **0.514** |
| Recall@10 | 0.276 | **0.532** |
| Recall@50 | 0.359 | **0.611** |
| Bundle recall | 0.206 | **0.521** |
| Bundle precision | 0.118 | **0.162** |
| Bundle recall, one-skill tasks | 0.312 | **0.750** |
| Bundle recall, multi-skill tasks | 0.177 | **0.460** |
| Capabilities per bundle (mean) | ~4.5 | **9.1** |
| Route, median / p95 (warm, in-process) | 19.9 s / 33.4 s | **2.5 s / 4.7 s** |
| Rebuild | 42 s | 100 s |

### 26,000 skills (every correct skill and look-alike, plus a seeded sample)

| | Before | After |
|---|--:|--:|
| Hit@1 | 0.347 | **0.653** |
| MRR | 0.415 | **0.703** |
| nDCG@10 | 0.310 | **0.563** |
| Recall@10 | 0.332 | **0.586** |
| Recall@50 | 0.443 | **0.657** |
| Bundle recall | 0.220 | **0.577** |
| Bundle precision | 0.132 | **0.179** |
| Capabilities per bundle (mean) | ~4.5 | **9.0** |
| Route, median / p95 (warm, in-process) | 6.5 s / 11.1 s | **0.69 s / 1.3 s** |
| Rebuild | 14 s | 29 s |

### What moved the numbers (26k, cumulative)

| Change | Hit@1 | Recall@10 | Bundle recall |
|---|--:|--:|--:|
| Before | 0.347 | 0.332 | 0.220 |
| Different skills that share a name are no longer hidden | 0.347 | 0.352 | 0.220 |
| Prompt tokenization: sentence punctuation, dotted filenames (`packets.pcap` → `pcap`), function words | 0.347 | 0.392 | 0.290 |
| Plural forms match (`structures` ↔ `structure`); umlaut words match at all | 0.387 | 0.424 | 0.318 |
| Rare terms weigh more (graded IDF) | 0.440 | 0.465 | 0.332 |
| Breadth of match counts linearly, not quadratically | 0.507 | 0.491 | 0.366 |
| Body keywords: each skill's 24 most distinctive body words (tf-idf) | **0.653** | **0.586** | 0.447 |
| Bundles fill to the default size of 10 with close matches (below) | **0.653** | **0.586** | **0.577** |

Speed came from indexing lexical ranking so a long task only touches the records it
can match, and routing long prompts instead of refusing them (they used to fail above
64 words).

### Bundle size

Routes used to stop adding extra matches once a bundle held four capabilities, so
bundles averaged about 4.5 whatever the cap. Now they fill toward the bundle size (10
by default, [configurable](CONFIGURATION.md#bundle-size)) with candidates scoring at
least half as well as the best match. The cutoff was picked from this sweep (26k
skills, size 10):

| Extra matches allowed | Capabilities per bundle | Bundle recall | Bundle precision | One-skill tasks | Multi-skill tasks |
|---|--:|--:|--:|--:|--:|
| Old behavior (stop at 4) | 4.5 | 0.447 | 0.254 | 0.750 | 0.364 |
| Any score | 10.0 | 0.580 | 0.151 | 0.812 | 0.517 |
| ≥ 45% of the best match | 9.3 | 0.580 | 0.170 | 0.812 | 0.517 |
| **≥ 50% of the best match (default)** | **9.0** | **0.577** | **0.179** | **0.812** | **0.513** |
| ≥ 55% of the best match | 8.6 | 0.573 | 0.188 | 0.812 | 0.508 |
| ≥ 60% of the best match | 8.1 | 0.559 | 0.209 | 0.812 | 0.491 |

On 79k skills the default gives 0.521 bundle recall (was 0.417) with 9.1 capabilities
per bundle. Ranking (Hit@1, MRR, Recall@K) and speed don't change.

The cost is context: an agent that reads every routed skill reads about twice as much
skill text (a median of ~16,000 tokens on the 79k pool for six everyday tasks, against
~157M for the whole pool). Set a smaller size if that matters more than coverage.

## Compared with the SkillRouter paper

Hit@1 on the hard tier (~80k skills), from the paper's tables, next to Lockkeeper on
the same pool:

| System | Model size | Hit@1 (hard) |
|---|--:|--:|
| BM25 | – | 0.280 |
| Lockkeeper before | – | 0.253 |
| Qwen3-Embedding-0.6B | 0.6B | 0.533 |
| **Lockkeeper after (standard library only)** | **–** | **0.547** |
| gemini-embedding-001 | – | 0.560 |
| BGE-Large-v1.5 | 335M | 0.587 |
| text-embedding-3-large | – | 0.600 |
| Qwen3-Embedding-8B | 8B | 0.627 |
| SR-Emb-0.6B (fine-tuned) | 0.6B | 0.640 |
| SkillRouter pipeline (fine-tuned embedder + reranker) | 1.2B | 0.720 |

Lockkeeper's ranker, with no model, sits among the paper's general-purpose embedding
models and well above BM25. Fine-tuned retrieve-and-rerank models remain stronger;
they need a GPU or seconds per route on a CPU. The comparison isn't exact: the paper
embeds full skill bodies, while Lockkeeper keeps 24 keywords per body.

## Optional decision models

The decision-provider stage lets a model reorder Lockkeeper's top 24 candidates. These
were measured on 26k skills with the *earlier* ranking (Hit@1 0.347), using the
corrected shortlist-only blend:

| Provider | Hit@1 | MRR | Time per route (CPU, 24 candidates) |
|---|--:|--:|--:|
| none | 0.347 | 0.415 | – |
| MiniLM-L6 cross-encoder | 0.387 | 0.464 | ~2.6 s |
| Laya (english) | 0.387 | 0.447 | ~35 s |
| SkillRouter SR-Rank-0.6B (38 of 75 tasks) | +8 points | | ~31–42 s |

A reranker can only reorder what the first stage found; at that time only ~38% of
the correct skills were in the top 24. Improving the first stage (the table above)
moved the numbers far more than any reranker, at no runtime cost.

## Reproduce

```sh
# ~400 MB download, once (pinned dataset revision)
python3 scripts/bench_routing.py prepare --home /tmp/lk-26k --size 26000
python3 scripts/bench_routing.py run --home /tmp/lk-26k --json report-26k.json

python3 scripts/bench_routing.py prepare --home /tmp/lk-79k            # the whole hard tier
python3 scripts/bench_routing.py run --home /tmp/lk-79k

# with a decision provider
python3 scripts/bench_routing.py run --home /tmp/lk-26k --provider-json \
  '{"provider": "sidecar", "backend": "cross-encoder"}'
```

`prepare` only ever writes into the directory you give it, and refuses a non-empty
directory it didn't create.

## Caveats

- **75 tasks is a small sample.** One task is 1.3 points of Hit@1; treat differences
  under ~5 points as noise.
- **The changes were developed against this benchmark** (on the 26k sample), so the
  "after" numbers are not a held-out result. Every change is general (tokenization,
  plural matching, IDF weighting, reading skill bodies), each was checked against the
  existing test suite of real-world routing cases, and the 79k run was only used as a
  final check, but it uses the same 75 tasks.
- **SkillsBench tasks are long and technical.** Short everyday prompts ("review this
  PR") behave differently; `scripts/eval_decision.py` evaluates labeled tasks from your
  own history.
- **Identical copies count as misses.** Some correct skills have exact duplicates in
  the pool (same name and description); Lockkeeper shows one copy, which may not be
  the labeled one.
- **Body keywords double rebuild time** on very large registries (100 s at 79k skills,
  a few seconds at typical sizes). Routing stays fast; only re-indexing is slower.
