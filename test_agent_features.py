"""Portable regression tests with generated containers; no Ascend installation needed."""
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest

PERFP = Path(__file__).with_name("perfp")


def record(kind, data):
    payload = json.dumps(data).encode()
    return struct.pack("<IIBB2s", len(payload), 0, kind, 0, b"\x00\x5a") + payload


def event(phase, ts, dur=None, **fields):
    value = {"ph": phase, "ts": ts, "pid": "core0", "tid": "MTE2", "name": "COPY"}
    if dur is not None:
        value["dur"] = dur
    value.update(fields)
    return value


class AgentFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.input = self.root / "visualize_data.bin"
        self.out = self.root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def make_input(self, events, calls=None):
        payload = record(2, {"traceEvents": events})
        if calls is not None:
            payload += record(4, {"Cores": ["core0"], "Instructions": [{
                "Address": "0x1", "Source": "COPY", "Pipe": "MTE2",
                "Instructions Executed": [calls], "Cycles": [3],
            }]})
        self.input.write_bytes(payload)

    def run_perfp(self, *args):
        return subprocess.run([sys.executable, str(PERFP), *map(str, args)],
                              capture_output=True, text=True)

    def build(self):
        proc = self.run_perfp(self.input, "-o", self.out, "--quiet")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads((self.out / "summary.json").read_text())

    def test_time_extent_excludes_metadata_and_includes_completion(self):
        self.make_input([event("M", -100), event("X", 10, 20, args={"code": "/kernel.asc:10"}),
                         event("X", 15, 2, args={"code": "/kernel.asc:11"})], calls=2)
        summary = self.build()
        self.assertEqual(summary["trace"]["event_start_range_us"], 5)
        self.assertEqual(summary["trace"]["event_coverage_us"], 20)
        self.assertIsNone(summary["trace"]["device_task_duration_us"])
        self.assertEqual(summary["quality"]["status"], "ok")
        self.assertIn("Deprecated", summary["metric_semantics"]["wall_us"])
        self.assertEqual(hashlib.sha256(self.input.read_bytes()).hexdigest(),
                         json.loads((self.out / "summary.json").read_text())["input_sha256"])
        self.assertLess((self.out / "overview.md").stat().st_size, 5000)

    def test_count_delta_has_no_speculative_cause(self):
        self.make_input([event("X", 1, 2)], calls=2)
        summary = self.build()
        self.assertEqual(summary["quality"]["execution_count_delta"], -1)
        self.assertEqual(summary["quality"]["execution_count_delta_pct"], -50)
        self.assertEqual(summary["quality"]["per_core_accounting"][0]["delta"], -1)
        self.assertTrue(any("Cause is unknown" in w for w in summary["warnings"]))

    def test_missing_counters_and_source_are_explicit(self):
        self.make_input([event("X", 1, 2)])
        summary = self.build()
        self.assertFalse(summary["quality"]["instruction_counters_available"])
        self.assertIsNone(summary["quality"]["execution_count_delta"])
        self.assertEqual(summary["quality"]["x_events_without_source_stack"], 1)
        self.assertEqual(summary["quality"]["x_source_stack_coverage_pct"], 0)

    def test_zero_counters_report_mismatch_without_division_by_zero(self):
        self.make_input([event("X", 1, 2)], calls=0)
        summary = self.build()
        self.assertEqual(summary["quality"]["execution_count_delta"], 1)
        self.assertIsNone(summary["quality"]["execution_count_delta_pct"])
        self.assertTrue(any("zero counter total" in w for w in summary["warnings"]))

    def test_nested_sync_pairs(self):
        self.make_input([event("B", 1, id=1), event("B", 2, id=1),
                         event("E", 3, id=1), event("E", 4, id=1)], calls=2)
        summary = self.build()
        self.assertEqual(summary["trace"]["sync_spans"], 2)
        self.assertEqual(summary["quality"]["unmatched_sync_begins"], 0)
        with sqlite3.connect(self.out / "profile.sqlite") as db:
            self.assertEqual(db.execute("SELECT SUM(dur) FROM spans").fetchone()[0], 4)

    def test_sync_pair_quality_and_negative_duration(self):
        self.make_input([event("B", 1, id=1), event("E", 2, id=1),
                         event("B", 3, id=2), event("E", 4, id=3),
                         event("B", 8, id=4), event("E", 7, id=4),
                         event("X", 5, -2)], calls=2)
        q = self.build()["quality"]
        self.assertEqual(q["unmatched_sync_begins"], 1)
        self.assertEqual(q["unmatched_sync_ends"], 1)
        self.assertEqual(q["invalid_sync_spans"], 1)
        self.assertEqual(q["invalid_time_events"], 1)
        with sqlite3.connect(self.out / "profile.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM spans").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_metadata_only_has_no_time_extent(self):
        self.make_input([event("M", 0)])
        self.assertIsNone(self.build()["trace"]["event_coverage_us"])

    def test_batch_builds_all_kernels_and_preserves_failure(self):
        self.make_input([event("X", 1, 2)])
        collection = self.root / "collection"
        for name in ("kernel1", "kernel2", "bad"):
            folder = collection / name
            folder.mkdir(parents=True)
            (folder / "visualize_data.bin").write_bytes(
                b"not a container" if name == "bad" else self.input.read_bytes())
        ambiguous = self.run_perfp(collection, "-o", self.out)
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("--all", ambiguous.stderr)
        batch = self.run_perfp(collection, "--all", "-o", self.out, "--quiet")
        self.assertEqual(batch.returncode, 1)
        entries = json.loads((self.out / "index.json").read_text())["bundles"]
        self.assertEqual([e["status"] for e in entries].count("success"), 2)
        self.assertEqual([e["status"] for e in entries].count("failed"), 1)

    def test_force_cannot_delete_input(self):
        self.make_input([event("X", 1, 2)])
        before = self.input.read_bytes()
        proc = self.run_perfp(self.input, "-o", self.root, "--force")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.input.read_bytes(), before)

    def test_repeated_trace_is_not_silently_dropped(self):
        trace = record(2, {"traceEvents": [event("X", 1, 2)]})
        self.input.write_bytes(trace + trace)
        proc = self.run_perfp(self.input, "-o", self.out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("multiple trace", proc.stderr)

    def test_bad_cli_values(self):
        self.make_input([event("X", 1, 2)])
        for flag, value in (("--bucket-us", "0"), ("--bucket-us", "nan"), ("--top", "0")):
            self.assertEqual(self.run_perfp(self.input, flag, value).returncode, 2)


if __name__ == "__main__":
    unittest.main()
