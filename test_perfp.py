#!/usr/bin/env python3
"""Smoke test for perfp.  Standard library only.

Usage:
    python3 test_perfp.py
    PERFP_SAMPLE=/path/to/case python3 test_perfp.py

If the sample case cannot be found the test is skipped (exit 0).
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PERFP = os.path.join(HERE, "perfp")


def find_sample():
    env = os.environ.get("PERFP_SAMPLE")
    if env:
        return env
    guess = os.path.join(os.path.dirname(HERE), "smoke_p03")
    return guess if os.path.isdir(guess) else None


def run(args, **kw):
    return subprocess.run([sys.executable, PERFP] + args,
                          capture_output=True, text=True, **kw)


def main():
    sample = find_sample()
    if not sample:
        print("skip: sample case not found (set PERFP_SAMPLE)")
        return 0

    # 1. info
    p = run(["info", sample, "--json"])
    assert p.returncode == 0, p.stderr
    info = json.loads(p.stdout)
    assert len(info["records"]) == 31, len(info["records"])
    types = {}
    for r in info["records"]:
        types[r["type"]] = types.get(r["type"], 0) + 1
    assert types == {1: 28, 2: 1, 3: 1, 4: 1}, types
    print(f"ok info: {len(info['records'])} records, types={types}")

    tmp = tempfile.mkdtemp(prefix="perfp_test_")
    try:
        out = os.path.join(tmp, "out")
        p = run([sample, "-o", out, "--no-src", "--no-raw",
                 "--no-events-csv", "--quiet"])
        assert p.returncode == 0, p.stderr

        db = sqlite3.connect(os.path.join(out, "profile.sqlite"))
        cur = db.cursor()

        def one(sql):
            return cur.execute(sql).fetchone()[0]

        checks = {
            "events": (one("SELECT COUNT(*) FROM events"), 636694),
            "instructions": (one("SELECT COUNT(*) FROM instructions"), 2354),
            "instruction_core": (one("SELECT COUNT(*) FROM instruction_core"), 89452),
            "cores": (one("SELECT COUNT(*) FROM cores"), 38),
            "spans": (one("SELECT COUNT(*) FROM spans"), 10108),
            "pcs": (one("SELECT COUNT(*) FROM pcs"), 2354),
        }
        for name, (got, want) in checks.items():
            assert got == want, (name, got, want)
            print(f"ok {name}: {got}")

        assert one("SELECT COUNT(*) FROM code_stacks") == 980
        assert one("SELECT SUM(calls) FROM instruction_core") == 615528
        top = cur.execute(
            "SELECT mnemonic FROM v_instruction GROUP BY mnemonic "
            "ORDER BY SUM(trace_dur_us) DESC LIMIT 1").fetchone()[0]
        assert top == "MOVEMASK", top
        print(f"ok top mnemonic: {top}")

        summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
        assert "MOVEMASK" in summary and "## Warnings" in summary
        print(f"ok summary.md: {len(summary)} bytes")

        db.close()
        print("PASS")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
