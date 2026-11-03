"""Compare frozen A/B/C evaluation reports without private evaluation content."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


FORMAT = "wissensraum-evaluation-variant"
VERSION = 1
VARIANTS = ("A", "B", "C")
FREEZE_FIELDS = {
    "commit", "database_sha256", "evaluation_sha256", "documents", "chunks", "vectors",
    "answerable", "excluded", "embedding_model", "answer_model",
}
METRIC_FIELDS = {
    "true_accept", "true_reject", "false_accept", "false_reject", "recall_at_1",
    "recall_at_3", "recall_at_5", "mrr", "p95_ms", "technical_errors", "model_calls",
    "second_passes", "neighbor_sections",
}


def _non_negative_int(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _rate(value, field):
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError(f"{field} must be between 0 and 1")


def load_report(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if type(data) is not dict or set(data) != {"format", "version", "variant", "freeze", "metrics"}:
        raise ValueError(f"{path}: invalid report fields")
    if data["format"] != FORMAT or data["version"] != VERSION or data["variant"] not in VARIANTS:
        raise ValueError(f"{path}: invalid report identity")
    freeze, metrics = data["freeze"], data["metrics"]
    if type(freeze) is not dict or set(freeze) != FREEZE_FIELDS:
        raise ValueError(f"{path}: invalid freeze fields")
    if type(metrics) is not dict or set(metrics) != METRIC_FIELDS:
        raise ValueError(f"{path}: invalid metric fields")
    for field in ("documents", "chunks", "vectors", "answerable", "excluded"):
        _non_negative_int(freeze[field], f"freeze.{field}")
    for field in ("true_accept", "true_reject", "false_accept", "false_reject",
                  "technical_errors", "model_calls", "second_passes", "neighbor_sections"):
        _non_negative_int(metrics[field], f"metrics.{field}")
    for field in ("recall_at_1", "recall_at_3", "recall_at_5", "mrr"):
        _rate(metrics[field], f"metrics.{field}")
    if type(metrics["p95_ms"]) not in (int, float) or metrics["p95_ms"] < 0:
        raise ValueError("metrics.p95_ms must be non-negative")
    if metrics["true_accept"] + metrics["false_reject"] != freeze["answerable"]:
        raise ValueError(f"{path}: answerable confusion counts do not match freeze")
    if metrics["true_reject"] + metrics["false_accept"] != freeze["excluded"]:
        raise ValueError(f"{path}: exclusion confusion counts do not match freeze")
    if not metrics["recall_at_1"] <= metrics["recall_at_3"] <= metrics["recall_at_5"]:
        raise ValueError(f"{path}: recall must be monotonic")
    return data


def limits(freeze):
    return {
        "false_reject": math.floor(freeze["answerable"] * 0.10 + 1e-9),
        "false_accept": math.floor(freeze["excluded"] * 0.05 + 1e-9),
        "technical_errors": 0,
    }


def compare(reports):
    if len(reports) != len(VARIANTS) or {report["variant"] for report in reports} != set(VARIANTS):
        raise ValueError("exactly one report for each variant A, B and C is required")
    baseline = reports[0]["freeze"]
    if any(report["freeze"] != baseline for report in reports[1:]):
        raise ValueError("variant reports do not share identical freeze metadata")
    if baseline["chunks"] != baseline["vectors"]:
        raise ValueError("frozen stock has incomplete vectors")
    gate_limits = limits(baseline)
    rows = []
    for report in sorted(reports, key=lambda item: item["variant"]):
        metrics = report["metrics"]
        passed = all(metrics[name] <= maximum for name, maximum in gate_limits.items())
        rows.append({"variant": report["variant"], "passed": passed, **metrics})
    eligible = [row for row in rows if row["passed"]]
    winner = min(eligible, key=lambda row: (
        row["false_reject"], row["false_accept"], -row["mrr"], row["p95_ms"],
        row["model_calls"], row["second_passes"], row["neighbor_sections"], row["variant"],
    ))["variant"] if eligible else None
    return {
        "format": "wissensraum-evaluation-comparison",
        "version": 1,
        "freeze": baseline,
        "limits": gate_limits,
        "variants": rows,
        "winner": winner,
        "gate_passed": winner is not None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs=3, type=Path, metavar="REPORT")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = compare([load_report(path) for path in args.reports])
    payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
