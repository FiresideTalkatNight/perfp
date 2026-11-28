"""Regression tests for pipe-gap, wait-attribution, DMA and compare features.

Uses only generated containers; no CANN or real sample is required.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_agent_features import PERFP, event, record


class AnalysisFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_input(self, name, dma_bytes=(1024, 4096), dma_calls=(1, 2)):
        root = self.root / name
        root.mkdir()
        trace = [
            event("M", -100),
            event("X", 1.0, 2.0, pid="core0", tid="MTE2",
                  name="MOV_OUT_TO_UB",
                  args={"code": "/k.asc:10", "pc_addr": "0x1",
                        "detail": "Src:OUT,Dst:UB,XN:X1=0x1000"}),
            event("X", 1.5, 4.0, pid="core0", tid="VECTOR",
                  name="VMUL", args={"code": "/k.asc:20"}),
            event("B", 1.5, pid="core0", tid="VECTOR", name="WAIT_FLAG",
                  id="g1", args={"code": "/k.asc:20"}),
            event("B", 2.9, pid="core0", tid="MTE2", name="SET_FLAG",
                  id="g1", args={"code": "/k.asc:10"}),
            event("E", 3.0, pid="core0", tid="MTE2", name="SET_FLAG", id="g1"),
            event("E", 3.0, pid="core0", tid="VECTOR", name="WAIT_FLAG", id="g1"),
            event("B", 3.2, pid="core0", tid="VECTOR", name="WAIT_FLAG",
                  id="g2", args={"code": "/k.asc:20"}),
            event("X", 5.0, 2.0, pid="core0", tid="MTE2",
                  name="MOV_OUT_TO_UB",
                  args={"code": "/k.asc:30", "pc_addr": "0x2",
                        "detail": "Src:OUT,Dst:UB,XN:X1=0x2000"}),
            event("B", 6.9, pid="core0", tid="MTE2", name="SET_FLAG",
                  id="g2", args={"code": "/k.asc:30"}),
            event("E", 7.0, pid="core0", tid="MTE2", name="SET_FLAG", id="g2"),
            event("E", 7.0, pid="core0", tid="VECTOR", name="WAIT_FLAG", id="g2"),
        ]
        payload = record(2, {"traceEvents": trace})
        payload += record(4, {
            "Cores": ["core0"],
            "Instructions": [
                {"Address": "0x1", "Source": "MOV_OUT_TO_UB", "Pipe": "MTE2",
                 "Instructions Executed": [dma_calls[0]],
                 "Cycles": [10], "Process Bytes": [dma_bytes[0]]},
                {"Address": "0x2", "Source": "MOV_OUT_TO_UB", "Pipe": "MTE2",
                 "Instructions Executed": [dma_calls[1]],
                 "Cycles": [20], "Process Bytes": [dma_bytes[1]]},
            ],
        })
        binp = root / "visualize_data.bin"
        binp.write_bytes(payload)
        return binp

    def build(self, name, **kw):
        binp = self.make_input(name, **kw)
        out = self.root / (name + "_out")
        proc = subprocess.run(
            [sys.executable, str(PERFP), str(binp), "-o", str(out),
             "--quiet", "--no-events-csv"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out

    def test_wait_is_attributed_to_dma_work_and_gap(self):
        out = self.build("a")
        db = sqlite3.connect(out / "profile.sqlite")
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM v_wait_attribution ORDER BY wait_ts").fetchall()
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["producer_pipe"], "MTE2")
        self.assertEqual(first["producer_event_id"], 1)  # first MTE2 X event
        self.assertEqual(first["status"], "blocked_on_producer")
        self.assertAlmostEqual(first["delay_us"], 1.5, places=6)
        self.assertEqual(first["blocks_on_dma"], 1)
        self.assertAlmostEqual(second["delay_us"], 3.8, places=6)

        gap = db.execute(
            "SELECT * FROM pipe_gaps WHERE pipe_id=("
            "SELECT id FROM pipes WHERE name='MTE2') ORDER BY ts").fetchone()
        self.assertAlmostEqual(gap["ts"], 3.0, places=6)
        self.assertAlmostEqual(gap["dur"], 2.0, places=6)
        self.assertAlmostEqual(gap["attributed_us"], 1.8, places=6)
        self.assertEqual(gap["status"], "attributed")
        db.close()


    def test_duplicate_candidate(self):
        root = self.root / "dup"
        root.mkdir()
        detail = "Src:OUT,Dst:UB,XN:X1=0x1000,id:1"
        trace = [
            event("X", 1.0, 1.0, pid="core0", tid="MTE2",
                  name="MOV_OUT_TO_UB",
                  args={"code": "/k.asc:10", "pc_addr": "0x1",
                        "detail": detail}),
            event("X", 2.0, 1.0, pid="core0", tid="MTE2",
                  name="MOV_OUT_TO_UB",
                  args={"code": "/k.asc:10", "pc_addr": "0x1",
                        "detail": detail.replace("id:1", "id:2")}),
        ]
        payload = record(2, {"traceEvents": trace})
        payload += record(4, {
            "Cores": ["core0"],
            "Instructions": [{
                "Address": "0x1", "Source": "MOV_OUT_TO_UB", "Pipe": "MTE2",
                "Instructions Executed": [2], "Cycles": [10],
                "Process Bytes": [2048],
            }],
        })
        binp = root / "visualize_data.bin"
        binp.write_bytes(payload)
        out = root / "out"
        proc = subprocess.run(
            [sys.executable, str(PERFP), str(binp), "-o", str(out),
             "--quiet", "--no-events-csv"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        db = sqlite3.connect(out / "profile.sqlite")
        n, cls = db.execute(
            "SELECT duplicate_count, classification FROM dma_events "
            "ORDER BY ts LIMIT 1").fetchone()
        self.assertEqual(n, 2)
        self.assertEqual(cls, "duplicate_candidate")
        excess = db.execute(
            "SELECT SUM(bytes_inferred) - MIN(block_bytes) FROM v_dma_duplicate"
        ).fetchone()[0]
        self.assertAlmostEqual(excess, 1024)
        db.close()

    def test_dma_bytes_duplicate_and_compare(self):
        a = self.build("a", dma_bytes=(1024, 4096), dma_calls=(1, 2))
        b = self.build("b", dma_bytes=(1024, 6144), dma_calls=(1, 2))
        db = sqlite3.connect(a / "profile.sqlite")
        total = db.execute("SELECT SUM(block_bytes) FROM dma_events").fetchone()[0]
        self.assertAlmostEqual(total, 1024 + 2048)  # 4096 / 2 calls
        detail = db.execute(
            "SELECT direction, overlap_us FROM dma_events ORDER BY ts LIMIT 1"
        ).fetchone()
        self.assertEqual(detail[0], "OUT->UB")
        self.assertAlmostEqual(detail[1], 1.5, places=6)
        db.close()

        cmp_out = self.root / "cmp"
        proc = subprocess.run(
            [sys.executable, str(PERFP), "compare", str(a), str(b),
             "-o", str(cmp_out), "--device-us-a", "10", "--device-us-b", "9"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads((cmp_out / "compare.json").read_text())
        self.assertAlmostEqual(result["metrics"]["dma_bytes_inferred"]["delta"],
                               1024, places=6)
        self.assertEqual(result["device_task_timing"]["a_us"], 10)
        self.assertEqual(result["device_task_timing"]["source"], "cli")
        self.assertTrue((cmp_out / "compare.md").is_file())


if __name__ == "__main__":
    unittest.main()
