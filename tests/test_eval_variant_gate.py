import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import compare_variants as variant_gate


class VariantGateTests(unittest.TestCase):
    def report(self, variant, **changes):
        metrics = {
            "true_accept": 61, "true_reject": 23, "false_accept": 1, "false_reject": 5,
            "recall_at_1": 0.70, "recall_at_3": 0.85, "recall_at_5": 0.92,
            "mrr": 0.78, "p95_ms": 20000, "technical_errors": 0, "model_calls": 90,
            "second_passes": 4, "neighbor_sections": 40,
        }
        metrics.update(changes)
        return {
            "format": variant_gate.FORMAT, "version": 1, "variant": variant,
            "freeze": {
                "commit": "e0efa8ea", "database_sha256": "a" * 64,
                "evaluation_sha256": "b" * 64, "documents": 55, "chunks": 349,
                "vectors": 349, "answerable": 66, "excluded": 24,
                "embedding_model": "qwen3-embedding:0.6b", "answer_model": "qwen3:8b",
            },
            "metrics": metrics,
        }

    def test_selects_only_gate_passing_variant_by_fixed_order(self):
        reports = [
            self.report("A", false_reject=7, true_accept=59),
            self.report("B", false_reject=5, true_accept=61, mrr=0.80, p95_ms=22000),
            self.report("C", false_reject=5, true_accept=61, mrr=0.80, p95_ms=19000),
        ]
        result = variant_gate.compare(reports)
        self.assertEqual(result["limits"], {"false_reject": 6, "false_accept": 1,
                                             "technical_errors": 0})
        self.assertEqual(result["winner"], "C")
        self.assertFalse(result["variants"][0]["passed"])

    def test_no_winner_when_every_variant_fails(self):
        reports = [self.report(name, false_accept=2, true_reject=22) for name in "ABC"]
        result = variant_gate.compare(reports)
        self.assertFalse(result["gate_passed"])
        self.assertIsNone(result["winner"])

    def test_rejects_different_freeze_or_incomplete_vectors(self):
        reports = [self.report(name) for name in "ABC"]
        reports[1]["freeze"]["database_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "identical freeze"):
            variant_gate.compare(reports)
        reports[1]["freeze"] = dict(reports[0]["freeze"])
        for report in reports:
            report["freeze"]["vectors"] = 348
        with self.assertRaisesRegex(ValueError, "incomplete vectors"):
            variant_gate.compare(reports)

    def test_load_rejects_bad_counts_and_non_monotonic_recall(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.json"
            report = self.report("A", true_accept=60)
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "confusion counts"):
                variant_gate.load_report(path)
            report = self.report("A", recall_at_3=0.60)
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "monotonic"):
                variant_gate.load_report(path)


if __name__ == "__main__":
    unittest.main()
