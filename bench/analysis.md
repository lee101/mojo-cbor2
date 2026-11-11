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
| encode 16 MiB bytes | native `copy_bytes` + 4-thread fan-out | `src/capi.mojo` |
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

## 3. Notes for the next pass

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
