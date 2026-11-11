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
of at least 4 MiB are divided among four concurrent calls to the native SIMD
copy kernel; smaller inputs remain serial to avoid launch overhead.

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

## License

MIT. See [LICENSE](LICENSE).
