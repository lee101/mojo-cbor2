from __future__ import annotations

import array
import ctypes
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
LIB = os.path.join(ROOT, "dist", "libmojo-cbor2.so")

I = ctypes.c_int64
_U64_ITEMSIZE = ctypes.sizeof(ctypes.c_uint64)

_instance: ctypes.CDLL | None = None
_parallel_ready = False
_pybytes_ptr = ctypes.pythonapi.PyBytes_AsString
_pybytes_ptr.argtypes = [ctypes.py_object]
_pybytes_ptr.restype = ctypes.c_void_p
_pybytes_new = ctypes.pythonapi.PyBytes_FromStringAndSize
_pybytes_new.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t]
_pybytes_new.restype = ctypes.py_object


def lib() -> ctypes.CDLL:
    global _instance
    if _instance is None:
        if not os.path.exists(LIB):
            subprocess.run(
                ["bash", os.path.join(ROOT, "build", "build.sh")],
                cwd=ROOT,
                check=True,
            )
        _instance = ctypes.CDLL(LIB)
        _instance.mcbor_encode.argtypes = [I, I, I, I, I]
        _instance.mcbor_encode.restype = I
        _instance.mcbor_uint_array_size.argtypes = [I, I]
        _instance.mcbor_uint_array_size.restype = I
        _instance.mcbor_encode_uint_array.argtypes = [I, I, I, I]
        _instance.mcbor_encode_uint_array.restype = I
        _instance.mcbor_decode_uint_array.argtypes = [I, I, I, I]
        _instance.mcbor_decode_uint_array.restype = I
        _instance.mcbor_scan.argtypes = [I, I, I, I, I]
        _instance.mcbor_scan.restype = I
    return _instance


def bytes_addr(value: bytes) -> int:
    address = _pybytes_ptr(value)
    if address is None:
        raise RuntimeError("could not obtain bytes buffer address")
    return int(address)


def writable_buffer_addr(value: memoryview) -> int:
    if not value.c_contiguous or value.itemsize != 1:
        raise TypeError("a contiguous byte buffer is required")
    if not value.nbytes:
        raise ValueError("cannot take the address of an empty buffer")
    return ctypes.addressof(ctypes.c_uint8.from_buffer(value))


def buffer_addr(value: bytes | memoryview) -> int:
    if isinstance(value, bytes):
        return bytes_addr(value)
    if value.readonly:
        raise TypeError("a writable or bytes-backed buffer is required")
    return writable_buffer_addr(value)


def tape_addr(value: array.array[int]) -> int:
    if value.typecode != "Q" or value.itemsize != _U64_ITEMSIZE:
        raise TypeError("native tapes require unsigned 64-bit array elements")
    if not value:
        raise ValueError("cannot take the address of an empty native tape")
    return ctypes.addressof((ctypes.c_uint64 * len(value)).from_buffer(value))


def _ensure_parallel_runtime():
    global _parallel_ready
    if not _parallel_ready:
        runtime = ctypes.CDLL(
            os.path.join(sys.prefix, "lib", "libKGENCompilerRTShared.so")
        )
        initialize = runtime.KGEN_CompilerRT_AsyncRT_GetOrCreateCPUDevice
        initialize.argtypes = []
        initialize.restype = ctypes.c_void_p
        if not initialize():
            raise RuntimeError("failed to initialize the Mojo CPU runtime")
        _parallel_ready = True


def _output_buffer(size: int) -> tuple[bytes, int]:
    if size < 0 or size > sys.maxsize:
        raise OverflowError("native output size is out of range")
    target = _pybytes_new(None, size)
    return target, bytes_addr(target)


def encode(
    tape: array.array[int], output_size: int, payloads: bytes = b""
) -> bytes:
    if len(tape) % 4:
        raise ValueError("native encoder tape must contain four-word records")
    if not tape:
        raise ValueError("native encoder tape cannot be empty")
    if output_size >= 1 << 20:
        _ensure_parallel_runtime()
    target, target_addr = _output_buffer(output_size)
    written = lib().mcbor_encode(
        tape_addr(tape),
        len(tape) // 4,
        bytes_addr(payloads),
        target_addr,
        output_size,
    )
    if written < 0:
        raise RuntimeError(f"native CBOR encoder failed with status {written}")
    if written != output_size:
        raise RuntimeError("native CBOR encoder returned an inconsistent size")
    return target


def encode_uint_array(values: array.array[int]) -> bytes:
    address = tape_addr(values)
    output_size = lib().mcbor_uint_array_size(address, len(values))
    if output_size < 0:
        raise RuntimeError(
            f"native unsigned array sizing failed with status {output_size}"
        )
    target, target_addr = _output_buffer(output_size)
    written = lib().mcbor_encode_uint_array(
        address, len(values), target_addr, output_size
    )
    if written != output_size:
        raise RuntimeError("native unsigned array encoder failed")
    return target


def decode_uint_array(
    data: bytes | memoryview, count: int
) -> tuple[list[int], int] | None:
    if count <= 0:
        return None
    values = array.array("Q", [0]) * count
    consumed = lib().mcbor_decode_uint_array(
        buffer_addr(data), len(data), tape_addr(values), count
    )
    if consumed < 0:
        return None
    return values.tolist(), consumed


def scan(data: bytes | memoryview, allow_indefinite: bool) -> array.array[int]:
    capacity = min(max(len(data) // 8, 64), 4096)
    tape = array.array("Q", [0]) * (capacity * 4)
    count = lib().mcbor_scan(
        buffer_addr(data), len(data), tape_addr(tape), capacity, allow_indefinite
    )
    if count == -1:
        raise EOFError
    if count < 0:
        raise ValueError("invalid CBOR initial byte or additional information")
    if count > capacity:
        tape = array.array("Q", [0]) * (count * 4)
        second = lib().mcbor_scan(
            buffer_addr(data), len(data), tape_addr(tape), count, allow_indefinite
        )
        if second != count:
            raise RuntimeError("native CBOR scanner returned inconsistent results")
    else:
        del tape[count * 4 :]
    return tape
