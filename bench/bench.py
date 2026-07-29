from __future__ import annotations

import os
import platform
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_DIR = os.path.join(ROOT, "python")

sys.path = [p for p in sys.path if os.path.abspath(p or ".") != PYTHON_DIR]
import cbor2 as upstream_cbor2

if os.path.abspath(upstream_cbor2.__file__).startswith(PYTHON_DIR):
    raise RuntimeError("benchmark failed to import the installed upstream cbor2")

for module_name in list(sys.modules):
    if module_name == "cbor2" or module_name.startswith("cbor2."):
        del sys.modules[module_name]
sys.path.insert(0, PYTHON_DIR)
import cbor2 as mojo_cbor2


def timeit(fn, minimum=0.35):
    best = float("inf")
    calls = 0
    start = time.perf_counter()
    while time.perf_counter() - start < minimum or calls < 3:
        tick = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - tick)
        calls += 1
    return best, result


def bench(name, mojo_fn, upstream_fn):
    mojo_time, mojo_value = timeit(mojo_fn)
    upstream_time, upstream_value = timeit(upstream_fn)
    if mojo_value != upstream_value:
        raise AssertionError(f"benchmark outputs differ for {name}")
    return name, mojo_time, upstream_time


def main():
    integers = list(range(200_000))
    records = [
        {"id": i, "active": i % 3 == 0, "name": f"record-{i}"}
        for i in range(30_000)
    ]
    blob = bytes(range(256)) * 65_536

    encoded_integers = upstream_cbor2.dumps(integers)
    encoded_records = upstream_cbor2.dumps(records)
    encoded_blob = upstream_cbor2.dumps(blob)

    rows = [
        bench(
            "encode 200k integers",
            lambda: mojo_cbor2.dumps(integers),
            lambda: upstream_cbor2.dumps(integers),
        ),
        bench(
            "decode 200k integers",
            lambda: mojo_cbor2.loads(encoded_integers),
            lambda: upstream_cbor2.loads(encoded_integers),
        ),
        bench(
            "encode 30k records",
            lambda: mojo_cbor2.dumps(records),
            lambda: upstream_cbor2.dumps(records),
        ),
        bench(
            "decode 30k records",
            lambda: mojo_cbor2.loads(encoded_records),
            lambda: upstream_cbor2.loads(encoded_records),
        ),
        bench(
            "encode 16 MiB bytes",
            lambda: mojo_cbor2.dumps(blob),
            lambda: upstream_cbor2.dumps(blob),
        ),
        bench(
            "decode 16 MiB bytes",
            lambda: mojo_cbor2.loads(encoded_blob),
            lambda: upstream_cbor2.loads(encoded_blob),
        ),
    ]

    print(f"Machine: {platform.processor() or platform.machine()}; {platform.platform()}")
    print()
    print("| workload | mojo-cbor2 | cbor2 | relative |")
    print("|---|---:|---:|---:|")
    for name, mojo_time, upstream_time in rows:
        relative = upstream_time / mojo_time
        print(
            f"| {name} | {mojo_time * 1000:.2f} ms | "
            f"{upstream_time * 1000:.2f} ms | {relative:.2f}x |"
        )


if __name__ == "__main__":
    main()
