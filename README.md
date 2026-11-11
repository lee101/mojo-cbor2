# mojo-cbor2

`mojo-cbor2` is a standalone Mojo port of the compute-heavy wire-format core
of Python's [`cbor2`](https://pypi.org/project/cbor2/) package. It exposes a
Python module named `cbor2`, with compatible top-level names and function
signatures for the covered subset.

The project is intended as a practical codec, not a binding stub. Mojo scans,
validates, emits headers, performs endian-aware integer handling, and copies
payloads. Python retains responsibility for mapping Python's dynamic object
model to and from the native typed representation.

## Coverage

The following are supported:

- `dumps()`, `dump()`, `loads()`, and `load()`
- `CBOREncoder`, `CBORDecoder`, `CBORTag`, `CBORSimpleValue`, `undefined`, and
  the upstream exception hierarchy
- unsigned and negative integers, arbitrary-size integers, floats, byte and
  text strings, arrays, maps, booleans, null, undefined, and simple values
- definite and indefinite arrays, maps, byte strings, and text strings
- canonical map ordering and preferred-width canonical float encoding
- datetime, date, decimal, rational, UUID, regex, set, IP address, and IP
  network semantic tags
- custom type encoders, default encoders, tag hooks, object hooks, and custom
  semantic decoders
- immutable decoding, maximum-depth enforcement, duplicate-key policy,
  UTF-8 error policy, and sequential file decoding

The current subset does not implement value sharing or string references;
requesting `value_sharing=True` or `string_referencing=True` raises
`NotImplementedError`. Low-level specialized methods and stateful features of
upstream's extension classes beyond `encode()`, `encode_to_bytes()`, and
`decode()` are also outside the covered API.

## Install and run

The repository pins the tested Mojo nightly and provides all Python
dependencies through Pixi:

```bash
pixi install
pixi run build
pixi run test
```

The build produces `dist/libmojo-cbor2.so`.

## Usage

Run this from the repository root after `pixi install`:

```bash
pixi run python - <<'PY'
import cbor2

message = {
    "sensor": "lab-1",
    "samples": [18.5, 18.75, 19.0],
    "ok": True,
}

encoded = cbor2.dumps(message, canonical=True)
assert cbor2.loads(encoded) == message
print(encoded.hex())
PY
```

## How it works

Encoding traverses an object once in Python and produces a contiguous
instruction tape. Each instruction consists of four unsigned 64-bit words:
the CBOR kind, numeric argument, optional payload address, and payload length.
Small generated payloads are combined into one pinned buffer, while existing
bytes and writable NumPy-backed memoryviews remain zero-copy. One C-ABI call
passes the tape and a Python-owned output buffer to Mojo, which emits the
complete document. Large homogeneous unsigned-integer arrays use a compact
native path without constructing the general instruction tape. Large arrays of
flat primitive maps use guarded iterative tape construction, with the recursive
builder retained as the fallback. No allocator-owned memory crosses the FFI
boundary.

Decoding passes the input address as an integer to Mojo. The native scanner
checks initial bytes and bounds, decodes big-endian arguments, skips payloads,
and writes a four-word token tape containing kinds, arguments, source
positions, and payload offsets. Python then reconstructs dynamic containers
and semantic types. This division keeps the wire loop native while preserving
the exact Python-facing behavior expected from `cbor2`.

## Benchmarks

Measured with `pixi run bench`, which holds the repository's machine-wide
benchmark lock. Times are the best completed iteration after repeated runs.
Relative values above `1.00x` favor `mojo-cbor2`; values below `1.00x` favor
upstream.

Machine: `x86_64`; `Linux 6.8.0-136-generic`, glibc 2.39.

| workload | mojo-cbor2 | cbor2 | relative |
|---|---:|---:|---:|
| encode 200k integers | 11.55 ms | 57.49 ms | 4.98x |
| decode 200k integers | 3.56 ms | 9.59 ms | 2.69x |
| encode 30k records | 75.28 ms | 39.16 ms | 0.52x |
| decode 30k records | 94.49 ms | 29.59 ms | 0.31x |
| encode 16 MiB bytes | 1.16 ms | 6.94 ms | 6.00x |
| decode 16 MiB bytes | 1.31 ms | 16.87 ms | 12.85x |

Re-measured after the SIMD pass, three consecutive `pixi run bench` runs on the
same 72-core host under a load average of 14-20 (values are min-max of the three
runs; upstream's column moved with the load, so the relative column is noisy):

| workload | mojo-cbor2 (3 runs) | cbor2 (3 runs) | relative |
|---|---|---:|---:|
| encode 200k integers | 12.13 - 13.01 ms | 61.50 - 71.14 ms | 4.73x - 5.87x |
| decode 200k integers | 3.93 - 4.27 ms | 10.26 - 11.88 ms | 2.48x - 2.95x |
| encode 30k records | 82.49 - 87.09 ms | 41.05 - 46.81 ms | 0.49x - 0.54x |
| decode 30k records | 105.55 - 114.20 ms | 27.88 - 32.39 ms | 0.26x - 0.28x |
| encode 16 MiB bytes | 0.68 - 1.10 ms | 6.69 - 10.75 ms | 9.80x - 13.47x |
| decode 16 MiB bytes | 1.21 - 1.25 ms | 16.03 - 17.14 ms | 13.07x - 13.72x |

Upstream's mature C extension remains faster for object-dense documents because
Python still constructs and consumes this port's tape. Flat primitive map arrays
bypass recursive Python dispatch, homogeneous integer arrays use their compact
native path, and payload copies use SIMD with a scalar tail. Exact byte strings
of at least 8 MiB are divided among four host threads that each call the native
SIMD copy kernel; smaller inputs stay serial, because the fan-out only pays off
above that size (see the parallel phase below).

No GPU path is included. CBOR header scanning is branch-heavy and payload copying
performs essentially no arithmetic per byte, far below the roughly two FLOPs per
byte threshold where device transfer and launch costs could be justified.

### SIMD pass: native copy kernel

The payload copy kernel in `src/capi.mojo` is the one loop in the native core
that both reads and writes the same data without per-element branching. It now
moves 256 bytes per step in 16-byte vectors (previously 32-byte steps of 8-byte
vectors), with a 16-byte remainder loop and a scalar tail.

Both kernels were built into one shared library and timed in the same process,
interleaved, on a 72-core host under a load average of 19-25. `speedup` is
`old / new`, so values above `1.00x` favor the new kernel. Ranges span three
runs.

| bytes copied | old kernel (min) | new kernel (min) | speedup (min) | speedup (median) |
|---|---:|---:|---:|---:|
| 64 KiB | 0.0093 ms | 0.0045 ms | 1.99x - 2.08x | 1.96x - 2.07x |
| 4 MiB | 0.524 ms | 0.353 ms | 1.48x - 1.52x | 1.48x - 1.53x |
| 16 MiB | 2.412 ms - 3.332 ms | 1.913 ms - 3.041 ms | 0.99x - 1.26x | 0.98x - 1.12x |

The 16 MiB case is memory-bandwidth bound under this load and shows no reliable
gain; 64 KiB and 4 MiB, the range where the kernel is CPU bound, are 1.5x-2.1x
faster. Against glibc `memmove` at the same sizes the new kernel measured
1.6x-1.9x faster while the destination was cache-resident. `pixi run bench`
reports `encode 16 MiB bytes` between 0.68 ms and 1.10 ms across runs
(9.8x-13.5x versus upstream); that spread is machine noise.

Two other ideas were measured and reverted because they were not faster at
caller sizes: fusing the unsigned-array sizing pass into the emitting pass with
SIMD loads (0.57 ms -> 0.87 ms for 200k mixed-magnitude integers) and replacing
the integer fast-path predicate in Python. `bench/analysis.md` records the full
table and the toolchain pitfalls found along the way.

The copy kernel is covered by tests at lengths 0, 1, 15, 16, 17, 255, 256, 257,
4095, 4096, 4097 and 70001, against destinations at offsets 0, 1, 3 and 8, plus
end-to-end nested-payload tests at the 256-byte block boundaries.

### Parallel phase: outcome

Two things were tried, both measured on the pinned toolchain. One was kept and
its threshold raised; the other was built, measured, and reverted.

#### Reverted: a Mojo-side `max.algorithm.parallelize` fan-out

`MOJO_NOTES` section 4 says `from max.algorithm import parallelize` is the live
API, so the obvious move is a native fan-out inside `src/capi.mojo`. That was
implemented: a `def work(i: Int) {imm}` worker copying one contiguous chunk,
launched with `parallelize(work, w, w)` behind an `n // 1 MiB >= w` clamp, plus a
mode whose workers do nothing so the launch/join cost could be measured on its
own. The `max` package (`max ==26.7.0.dev2026092605`, matching the Mojo pin) had
to be added to `pixi.toml` for the import to resolve. It compiled and returned
correct bytes.

It loses. On this 72-core Xeon E5-2697 v4 (2 sockets, 2 NUMA nodes, 36 physical
cores) under a load average of 20 from other tenants, the fan-out costs about
4.5 ms per call whether the workers copy or do nothing, at every worker count
from 2 to 8. The copy itself needs less than that until roughly 64 MiB, so the
fan-out is slower than serial across almost the whole range:

| bytes | serial ms | fan-out ms (best w) | speedup | idle fan-out ms |
|---:|---:|---:|---:|---:|
| 256 KiB | 0.0276 | 0.0235 (w=3) | 1.17x | 4.65 |
| 1 MiB | 0.0900 | 0.0863 (w=1) | 1.04x | 4.58 |
| 2 MiB | 0.1865 | 0.1828 (w=1) | 1.02x | 4.71 |
| 4 MiB | 0.3430 | 0.3532 (w=1) | 0.97x | 5.01 |
| 16 MiB | 3.2793 | 3.4215 (w=1) | 0.96x | 4.91 |
| 32 MiB | 8.0291 | 8.2670 (w=1) | 0.97x | 5.34 |
| 64 MiB | 16.1152 | 15.2662 (w=1) | 1.06x | 4.49 |

`w=1` is the serial kernel. The per-worker curve shows the same thing: at 2 MiB
every worker count from 2 to 8 lands at 0.04x, at 4 MiB at 0.05-0.06x, and even
at 64 MiB the gain flattens at 0.85-0.98x with no worker count ahead. The one
apparent win, 256 KiB at 1.17x, is noise: it is a 4 microsecond difference
against a 4.5 ms launch that has not happened yet at that size, and it reverses
run to run.

The 4.5 ms is the cost of `parallelize` itself in this environment: the host's
GPU/shared-work memory allocator is not touched, workers are spun up per call,
and the host is oversubscribed. It is a hard floor under any native fan-out here
and it is larger than any payload copy shorter than about 64 MiB, so the native
fan-out was reverted: `src/capi.mojo` is back to its pre-phase form with no
`parallelize`, no measurement exports, and no chunk logic. The `max` dependency
is kept, because the toolchain notes require it for any `max.*` import and the
build pin should stay complete; nothing imports it.

The method is in `bench/parallel_probe.py`, which names the exports it needs and
how to restore them; it is not runnable against the shipped library by design.
Full numbers and the worker curve are in `bench/analysis.md`.

#### Kept, with a raised threshold: 8 MiB for the host-thread copy

The serial kernel is fast enough that at 4 MiB a thread-pool round trip buys
nothing, so the old 4 MiB threshold was too low. Switching between the two
paths inside the same process, with the same input, five trials each, best
median:

| bytes | serial ms | 4 threads ms | speedup |
|---:|---:|---:|---:|
| 4 MiB | 0.281 | 0.279 | 1.00x |
| 5 MiB | 0.342 | 0.316 | 1.08x |
| 6 MiB | 0.415 | 0.351 | 1.18x |
| 8 MiB | 0.544 | 0.317 | 1.72x |

The crossover is around 6-7 MiB, and the gain is only worth a thread pool from
about 8 MiB up. A wider sweep confirms the shape and shows the reversal below
the threshold:

| bytes | serial ms | 4 threads ms | speedup |
|---:|---:|---:|---:|
| 256 KiB | 0.042 | 0.046 | 0.92x |
| 512 KiB | 0.072 | 0.061 | 1.19x |
| 1 MiB | 0.112 | 0.119 | 0.94x |
| 2 MiB | 0.211 | 0.239 | 0.88x |
| 4 MiB | 0.747 | 0.646 | 1.16x |
| 8 MiB | 1.947 | 1.143 | 1.70x |

The sub-4 MiB rows are noise around 1.0x and flip sign between sizes, which is
why they stay serial. The worker count is 4. More was not measured, because the
fan-out is one call per worker into a memory-bound kernel and the host already
runs at a load average of 14-25 on 72 cores, so widening it would take capacity
from the other tenants without evidence of a gain.

The threshold now lives in one place, `_THREADED_COPY_THRESHOLD` in
`python/cbor2/_lib.py`, and `_encode` dispatches on it. `MCBOR_SERIAL=1` forces
the serial kernel inside `encode_bytes_parallel`, which is what the
serial-vs-threaded tests and `bench/serial_vs_threads.py` use. End-to-end
through `dumps()`, threaded path against the same build forced serial, one fresh
process per trial, 7 trials, median of 3 trial-minima:

| bytes | threaded ms | serial ms | speedup |
|---:|---:|---:|---:|
| 4 MiB | 0.276 | 0.260 | 0.94x |
| 5 MiB | 0.311 | 0.324 | 1.05x |
| 6 MiB | 0.410 | 0.401 | 0.98x |
| 7 MiB | 0.425 | 0.414 | 0.97x |
| 8 MiB | 0.296 | 0.489 | 1.65x |
| 12 MiB | 0.491 | 0.769 | 1.57x |
| 16 MiB | 0.638 | 1.055 | 1.65x |

Below 8 MiB the two paths are within 6% of each other and the sign flips by
size, so the old 4 MiB threshold was spending a thread-pool round trip for
nothing; at 8 MiB and up the fan-out wins 1.57x-1.65x. A separate whole-run
A/B of the 4 MiB and 8 MiB thresholds (a longer script, 196 s, under heavier
load) came out at 0.94x, 0.79x, 0.49x, 0.77x, 1.04x, 1.01x and 1.01x for 4, 5,
6, 8, 16, 32 and 64 MiB — same direction at and below 8 MiB, but flat above,
because both builds take the same threaded path there. Those two sets disagree
on the size of the 8-16 MiB gain by up to 1.6x, which is what this host's
memory bandwidth does run to run; the shipped claim is the smaller one, 1.57x.

`tests/test_parity.py` now checks that the serial and threaded paths emit
identical bytes at 4 MiB, 4 MiB + 17, 8 MiB, 8 MiB + 1 and 9 MiB, that payload
lengths 8 MiB +- 1 around the 2 MiB chunk boundary round-trip, and that payloads
below the threshold never reach `encode_bytes_parallel`.

## License

MIT. See [LICENSE](LICENSE).
