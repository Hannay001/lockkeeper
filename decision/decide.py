#!/usr/bin/env python3
"""Decision sidecar for Lockkeeper: judges task <-> capability relevance locally.

Runs OUT OF PROCESS in its own venv, like the semantic sidecar, so the router
stays standard-library only. Lockkeeper sends a shortlist it has already
retrieved, filtered, and policy-checked; this process returns one probability
per candidate. It never decides eligibility, deny rules, or portfolio size.

Protocol (version 1), one JSON object each way:

  stdin   {"protocol": 1, "task": str, "model": str, "clarification": bool,
           "untrusted_notice": str,
           "candidates": [{"id", "type", "name", "description"}, ...]}
  stdout  {"protocol": 1, "provider": str, "model": str,
           "scores": {id: probability}, "clarification": probability | null}

Backends (--backend):

  laya           In-process Laya (pip install laya). One typed `choice`
                 question per candidate, batched with Router.predict_batch.
                 Laya's own README reports its base checkpoints are near chance
                 zero-shot on typed decisions; treat it as a base to fine-tune on
                 your routing labels and measure it in shadow mode first.
  cross-encoder  fastembed TextCrossEncoder (pip install fastembed), a reranker
                 trained for query <-> passage relevance. Default model
                 Xenova/ms-marco-MiniLM-L-6-v2 (English, small);
                 jinaai/jina-reranker-v2-base-multilingual handles German.
  overlap        Dependency-free token overlap. A baseline for evaluation and a
                 smoke test for the protocol, not a quality reranker.

Any failure exits non-zero; the router then reports the provider as unavailable
and routes exactly as it would without it.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_CROSS_ENCODER = "Xenova/ms-marco-MiniLM-L-6-v2"
RELEVANCE_INSTRUCTIONS = "Would the capability described in capability_metadata materially help accomplish the task?"
RELEVANCE_CRITERIA = {
    "A": "yes, it directly helps accomplish the task",
    "B": "no, it is not needed for this task",
}
CLARIFICATION_QUESTION = {
    "clarify": {
        "type": "choice",
        "instructions": "Is the task too vague to choose tools for without asking the user a clarifying question?",
        "criteria": {
            "A": "yes, the task is too vague and needs clarification",
            "B": "no, the task is specific enough to act on",
        },
    }
}
STOPWORDS = {
    "a", "an", "and", "the", "to", "of", "for", "in", "on", "with", "our", "my", "is", "are", "be",
    "und", "der", "die", "das", "mit", "fuer", "für", "von", "zu",
}


def passage(candidate: dict[str, Any]) -> str:
    return f"{candidate.get('type', '')} {candidate.get('name', '')}: {candidate.get('description', '')}".strip()


def choice_probability(answer: Any) -> float | None:
    if not isinstance(answer, dict):
        return None
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and isinstance(probabilities.get("A"), (int, float)):
        return float(probabilities["A"])
    confidence = answer.get("confidence")
    if answer.get("choice") in ("A", "B") and isinstance(confidence, (int, float)):
        return float(confidence) if answer["choice"] == "A" else 1.0 - float(confidence)
    return None


def score_laya(request: dict[str, Any]) -> tuple[dict[str, float], float | None, str]:
    from laya import Router

    router = Router()
    model = request.get("model") or None
    question = {
        "relevant": {
            "type": "choice",
            "instructions": f"{RELEVANCE_INSTRUCTIONS} {request.get('untrusted_notice', '')}".strip(),
            "criteria": dict(RELEVANCE_CRITERIA),
        }
    }
    batch = []
    for candidate in request["candidates"]:
        item = {
            "state": {"task": request["task"], "capability_metadata": passage(candidate)},
            "questions": question,
        }
        if model:
            item["model"] = model
        batch.append(item)
    results = router.predict_batch(batch)
    scores = {}
    used_model = model or ""
    for candidate, result in zip(request["candidates"], results, strict=True):
        probability = choice_probability((result.get("answers") or {}).get("relevant"))
        if probability is not None:
            scores[candidate["id"]] = probability
        used_model = used_model or str((result.get("routing") or {}).get("model") or "")
    clarification = None
    if request.get("clarification"):
        kwargs = {"model": model} if model else {}
        answer = router.predict({"task": request["task"]}, CLARIFICATION_QUESTION, **kwargs)
        clarification = choice_probability((answer.get("answers") or {}).get("clarify"))
    return scores, clarification, f"laya:{used_model}" if used_model else "laya"


def score_cross_encoder(request: dict[str, Any]) -> tuple[dict[str, float], float | None, str]:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    model_name = request.get("model") or DEFAULT_CROSS_ENCODER
    encoder = TextCrossEncoder(model_name=model_name)
    candidates = request["candidates"]
    logits = list(encoder.rerank(request["task"], [passage(candidate) for candidate in candidates]))
    scores = {
        candidate["id"]: 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, float(logit)))))
        for candidate, logit in zip(candidates, logits, strict=True)
    }
    return scores, None, model_name


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9äöüß]+", text.lower())
        if len(token) > 2 and token not in STOPWORDS
    }


def score_overlap(request: dict[str, Any]) -> tuple[dict[str, float], float | None, str]:
    task = _tokens(request["task"])
    scores = {}
    for candidate in request["candidates"]:
        words = _tokens(passage(candidate))
        scores[candidate["id"]] = len(task & words) / len(task) if task else 0.0
    return scores, None, "token-overlap"


BACKENDS = {"laya": score_laya, "cross-encoder": score_cross_encoder, "overlap": score_overlap}


def validate(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL_VERSION:
        raise ValueError(f"expected a protocol {PROTOCOL_VERSION} request object")
    if not isinstance(request.get("task"), str) or not request["task"].strip():
        raise ValueError("request.task must be a non-empty string")
    candidates = request.get("candidates")
    if not isinstance(candidates, list) or not all(
        isinstance(item, dict) and isinstance(item.get("id"), str) for item in candidates
    ):
        raise ValueError("request.candidates must be a list of objects with string ids")
    return request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lockkeeper decision sidecar")
    parser.add_argument("--backend", choices=sorted(BACKENDS), required=True)
    args = parser.parse_args(argv)
    try:
        request = validate(json.loads(sys.stdin.read()))
        scores, clarification, model = BACKENDS[args.backend](request)
    except Exception as error:  # noqa: BLE001 - the router treats any failure as "unavailable"
        print(f"decision sidecar failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "provider": f"sidecar:{args.backend}",
                "model": model,
                # Six places: a cross-encoder's probabilities on long tasks can all be
                # below 0.01, and four places would erase the order among them.
                "scores": {key: round(max(0.0, min(1.0, value)), 6) for key, value in scores.items()},
                "clarification": None if clarification is None else round(clarification, 4),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
