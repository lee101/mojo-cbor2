"""Probe: can a Mojo-side `max.algorithm.parallelize` fan-out beat the serial
byte-copy kernel on this host?

This is the measurement that decided the phase outcome (see README, "Parallel
phase"). It runs the *reverted* design: a `parallelize` fan-out over `w`
contiguous chunks inside `src/capi.mojo`, timed against the serial kernel on
identical buffers in a fresh process per cell.

It needs the measurement exports that were removed with the reverted code, so
it is not runnable against the shipped library. To re-run it, restore the
reverted file and rebuild:

    git log --all --oneline -- src/capi.mojo   # find the parallel-phase commit
    pixi run python bench/parallel_probe.py

The recorded results are in README.md and bench/analysis.md; this file is kept
as the reproducible method, not as a passing benchmark.
"""

from __future__ import annotations

import ctypes
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
        "PROBE_SIZES",
        "262144,1048576,2097152,4194304,16777216,33554432,67108864",
    ).split(",")
]
WORKERS = [1, 2, 3, 4, 6, 8]
REPS = int(os.environ.get("PROBE_REPS", "25"))
TRIALS = int(os.environ.get("PROBE_TRIALS", "3"))

CHILD = """
import ctypes, json, sys, time
sys.path.insert(0, {py!r})
import cbor2._lib as _lib
I = ctypes.c_int64
lib = _lib.lib()
lib.mcbor_copy_bytes_parallel.argtypes = [I, I, I, I, I]
lib.mcbor_copy_bytes_parallel.restype = I
size, workers, mode = {size}, {workers}, {mode}
src = (ctypes.c_uint8 * size)()
dst = (ctypes.c_uint8 * size)()
pattern = bytes(range(251))
block = pattern * (size // 251) + bytes(range(size % 251))
ctypes.memmove(src, block, size)
sa, da = ctypes.addressof(src), ctypes.addressof(dst)
fn = lib.mcbor_copy_bytes_parallel
samples = []
for _ in range({reps}):
    tick = time.perf_counter()
    rc = fn(sa, da, size, workers, mode)
    samples.append(time.perf_counter() - tick)
    assert rc == size, rc
if mode != 2:
    assert ctypes.string_at(da, size) == block, "copy corrupted"
print(json.dumps(samples))
"""


def cell(size: int, workers: int, mode: int) -> float:
    code = CHILD.format(py=PY, size=size, workers=workers, mode=mode, reps=REPS)
    best = float("inf")
    for _ in range(TRIALS):
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        best = min(best, statistics.median(json.loads(out.stdout)))
    return best


def main():
    print(
        f"cores={os.cpu_count()} load={os.getloadavg()[0]:.1f} "
        f"reps={REPS} trials={TRIALS} (best trial median, one process per cell)"
    )
    print()
    print("| bytes | serial ms | fanout w | fanout ms | speedup | idle-launch ms |")
    print("|---:|---:|---:|---:|---:|---:|")
    for size in SIZES:
        serial = cell(size, 1, 0)
        fan = {w: cell(size, w, 1) for w in WORKERS}
        launch = cell(size, 4, 2)
        bw = min(fan, key=fan.get)
        print(
            f"| {size} | {serial * 1e3:.4f} | {bw} | {fan[bw] * 1e3:.4f} | "
            f"{serial / fan[bw]:.2f}x | {launch * 1e3:.4f} |"
        )
        sys.stdout.flush()

    print()
    print("worker curve, speedup over serial (>1 means the fan-out wins)")
    print()
    print("| bytes | " + " | ".join(f"w={w}" for w in WORKERS) + " |")
    print("|---:|" + "---:|" * len(WORKERS))
    for size in SIZES:
        serial = cell(size, 1, 0)
        print(
            f"| {size} | "
            + " | ".join(f"{serial / cell(size, w, 1):.2f}x" for w in WORKERS)
            + " |"
        )


if __name__ == "__main__":
    main()
