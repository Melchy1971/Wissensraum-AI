"""Self-check of the latency script for the running server (tests/bench/server_latency.py, B10)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'bench'))

import server_latency


class LatencyScriptTests(unittest.TestCase):
    def test_percentiles_and_https_only(self):
        values = list(range(1, 101))
        self.assertEqual((server_latency.percentile(values, 0.5), server_latency.percentile(values, 0.95),
                          server_latency.percentile(values, 1.0)), (51, 95, 100))
        with self.assertRaises(ValueError):
            server_latency.measure('http://wissen.example.test', 'sitzung', 'Wartung')


if __name__ == '__main__':
    unittest.main()
