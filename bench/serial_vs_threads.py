"""End-to-end serial vs threaded encode, in one process and on the same input.

`cbor2.dumps(big_bytes)` currently takes the 4-thread ThreadPoolExecutor path in
`python/cbor2/_lib.py`; set `MCBOR_SERIAL=1` to force the serial kernel. This
script encodes the same payload both ways (fresh subprocess per mode, so no
warm thread pool carries over), verifies the bytes are identical, and reports
the best timing.

Run with `pixi run python bench/serial_vs_threads.py`. Sizes are the sizes real
callers pass through `dumps()`, from below to well above the 4 MiB threshold.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "python")

SIZES = [
    int(v)
    for v in os.environ.get(
        "PROBE_SIZES", "1048576,4194304,8388608,16777216,33554432,67108864"
    ).split(",")
]
REPS = int(os.environ.get("PROBE_REPS", "9"))
TRIALS = int(os.environ.get("PROBE_TRIALS", "3"))


def run(size: int, serial: bool) -> dict:
    code = f"""
import hashlib, json, os, statistics, sys, time
sys.path.insert(0, {PY!r})
import cbor2
size = {size}
value = bytes(range(251)) * (size // 251) + bytes(range(size % 251))
samples = []
digest = None
for _ in range({REPS}):
    tick = time.perf_counter()
    out = cbor2.dumps(value)
    samples.append(time.perf_counter() - tick)
    d = hashlib.sha256(out).hexdigest()
    assert digest in (None, d), "dumps produced different bytes between calls"
    digest = d
assert cbor2.loads(out) == value
print(json.dumps({{"samples": samples, "digest": digest}}))
"""
    env = dict(os.environ)
    if serial:
        env["MCBOR_SERIAL"] = "1"
    else:
        env.pop("MCBOR_SERIAL", None)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return json.loads(out.stdout)


def best(size: int, serial: bool) -> tuple[float, str]:
    t, digest = float("inf"), ""
    for _ in range(TRIALS):
        got = run(size, serial)
        t = min(t, statistics.median(got["samples"]))
        digest = got["digest"]
    return t, digest


def main():
    print(f"reps={REPS} trials={TRIALS} (best trial median, one process per cell)")
    print()
    print("| bytes | serial ms | threads ms | speedup | identical bytes |")
    print("|---:|---:|---:|---:|---|")
    for size in SIZES:
        s, sd = best(size, True)
        p, pd = best(size, False)
        same = "yes" if sd == pd else "NO"
        print(
            f"| {size} | {s * 1e3:.3f} | {p * 1e3:.3f} | {s / p:.2f}x | {same} |"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
