# Bench analysis: what is hot, and what is worth vectorising

Inputs for the SIMD pass over `src/capi.mojo`. Everything below was measured on
the pinned toolchain (`mojo ==1.2.0.dev2026092605`) with `pixi run bench` and a
profiler, not taken from documentation.

## 1. Where the time actually goes

`cProfile` on the six benchmark workloads, `bench/bench.py` sizes:

| workload | dominant cost | location |
| --- | --- | --- |
| encode 200k integers | `all(type(v) is int for v in obj)` (10.8 ms) plus `array.array` conversion (2.4 ms), of 12.0 ms total | `python/cbor2/__init__.py`, `_encode` |
| decode 200k integers | `array('Q').tolist()` (16 ms of 18 ms over 3 calls); native decode ~1 ms per call | `_lib.decode_uint_array` |
| encode 30k records | Python tape builder (0.87 s of 1.34 s over 3 calls) | `_encode_flat_map_array` |
| decode 30k records | Python tape consumer | `_decode_flat_map_array` |
| encode 16 MiB bytes | native `copy_bytes` + 4-thread fan-out | `src/capi.mojo` (above 8 MiB), `python/cbor2/_lib.py` |
| decode 16 MiB bytes | `bytes` slice of an exact payload | `_Parser.parse` |

Only the last two ever reach a byte-copy kernel; the other four are Python
dispatch or a single C call and cannot be moved into a Mojo SIMD loop without
changing who owns the loop.

## 2. Candidate loops and the verdict

| loop | verdict | evidence |
| --- | --- | --- |
| `copy_bytes` payload copy | **vectorised, kept** | was 8-byte lanes; 16-byte lanes with 256-byte steps: 2.0x at 64 KiB, 1.5x at 4 MiB, ~1.0-1.26x at 16 MiB (bandwidth bound) vs the old kernel, same process |
| `mcbor_uint_array_size` + `mcbor_encode_uint_array` (two passes over the tape) | **fusing rejected** | fused SIMD-load sizing measured 0.87 ms -> 1.12 ms (small values), 0.57 -> 0.87 ms (mixed) at 200k; reverted |
| per-lane head-width SIMD in the encoder | **rejected** | needs an exact 1-9 byte per-lane value; `SIMD[DType.uint64, 4]` literal constructors and lanes at `2**63..2**64-1` do not round-trip through this compiler, so any wrong answer writes a wrong document |
| `mcbor_decode_uint_array` element loop | not vectorised | dependent control flow: each element's length decides the next position |
| `mcbor_scan` token loop | not vectorised | same dependent control flow |
| `_encode` int-fast-path predicate | left unchanged | an `array.array`-based rewrite measured neutral-to-slower in an interleaved A/B (min 10.96 ms old vs 11.33 ms new); reverted |

## 3. Parallel phase: what was measured and what shipped

A native fan-out in `src/capi.mojo` (`max.algorithm.parallelize`, one worker per
contiguous chunk, `def work(i: Int) {imm}`) was implemented and measured against
the serial kernel, one fresh process per cell, 25 reps, best of 3 trial medians.
Host: 72-core Xeon E5-2697 v4, 2 sockets, 2 NUMA nodes, load average ~20.

| bytes | serial ms | native fan-out ms (best w) | speedup |
|---:|---:|---:|---:|
| 256 KiB | 0.0276 | 0.0235 (w=3) | 1.17x |
| 1 MiB | 0.0900 | 0.0863 (w=1) | 1.04x |
| 2 MiB | 0.1865 | 0.1828 (w=1) | 1.02x |
| 4 MiB | 0.3430 | 0.3532 (w=1) | 0.97x |
| 16 MiB | 3.2793 | 3.4215 (w=1) | 0.96x |
| 32 MiB | 8.0291 | 8.2670 (w=1) | 0.97x |
| 64 MiB | 16.1152 | 15.2662 (w=1) | 1.06x |

`w=1` is serial. A fan-out whose workers copy nothing costs 4.49-5.34 ms at
every worker count from 2 to 8, so `parallelize` has a ~4.5 ms fixed cost here
that exceeds the copy time below ~64 MiB. Per-worker speedups were 0.04x at
2 MiB, 0.05-0.06x at 4 MiB, and 0.85-0.98x at 64 MiB, flattening rather than
rising with the worker count. Verdict: reverted, no `parallelize` in the
shipped library.

The host-thread `ThreadPoolExecutor` path (4 workers, each calling the native
SIMD kernel on its own chunk) was already present. Its threshold was measured
and moved from 4 MiB to 8 MiB:

| bytes | serial ms | 4 threads ms | speedup |
|---:|---:|---:|---:|
| 4 MiB | 0.281 | 0.279 | 1.00x |
| 5 MiB | 0.342 | 0.316 | 1.08x |
| 6 MiB | 0.415 | 0.351 | 1.18x |
| 8 MiB | 0.544 | 0.317 | 1.72x |

A second, longer sweep (7 trials of 3, fresh process per trial) gave 0.94x at
4 MiB, 1.05x at 5, 0.98x at 6, 0.97x at 7, 1.65x at 8, 1.57x at 12 and 1.65x at
16 MiB end-to-end. Crossover ~6-7 MiB; 8 MiB keeps a margin, and the shipped
number quoted in README is the smaller of the two estimates. Below the threshold the two
paths are within noise (0.88x-1.19x and sign-flipping), so those sizes stay
serial. Re-run with `bench/serial_vs_threads.py` after any kernel change.

## 4. Notes for the next pass

- `simd_width_of[DType.uint8]()` returns the lane *count* (16 on this AVX2 box),
  not bytes. Multiplying it by anything to form a byte offset silently changes
  the step size.
- `p.store(i + k * N, v)` scales the offset by the lane width; write
  `(p + k * N).unsafe_store[width=N](...)` instead to get one full-vector move.
- Vec-ops that need a compare (`v > c`) are lowered to scalar element loops on
  this toolchain, and run slower than the branchy scalar code they replace.
- Machine noise matters: sustained load of 15-25 on 72 cores shifts Python-side
  timings by 20% and memory-bound copies by 3-4x. Use same-process A/B and
  report it as such.
