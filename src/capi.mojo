"""CBOR wire encoder and scanner exposed through a small C ABI."""

from std.sys import simd_width_of

comptime BPtr = UnsafePointer[UInt8, AnyOrigin[mut=True]]
comptime U64Ptr = UnsafePointer[UInt64, AnyOrigin[mut=True]]


def write_head(dst: BPtr, pos: Int, major: Int, arg: UInt64) -> Int:
    var p = pos
    if arg < 24:
        dst[p] = UInt8((major << 5) | Int(arg))
        return p + 1
    if arg <= 0xFF:
        dst[p] = UInt8((major << 5) | 24)
        dst[p + 1] = UInt8(arg)
        return p + 2
    if arg <= 0xFFFF:
        dst[p] = UInt8((major << 5) | 25)
        dst[p + 1] = UInt8((arg >> 8) & 0xFF)
        dst[p + 2] = UInt8(arg & 0xFF)
        return p + 3
    if arg <= 0xFFFFFFFF:
        dst[p] = UInt8((major << 5) | 26)
        dst[p + 1] = UInt8((arg >> 24) & 0xFF)
        dst[p + 2] = UInt8((arg >> 16) & 0xFF)
        dst[p + 3] = UInt8((arg >> 8) & 0xFF)
        dst[p + 4] = UInt8(arg & 0xFF)
        return p + 5
    dst[p] = UInt8((major << 5) | 27)
    for i in range(8):
        dst[p + 1 + i] = UInt8((arg >> UInt64(56 - 8 * i)) & 0xFF)
    return p + 9


def head_size(arg: UInt64) -> Int:
    if arg < 24:
        return 1
    if arg <= 0xFF:
        return 2
    if arg <= 0xFFFF:
        return 3
    if arg <= 0xFFFFFFFF:
        return 5
    return 9


def copy_bytes(src: BPtr, dst: BPtr, n: Int):
    comptime W = simd_width_of[DType.float64]()
    var i = 0
    while i + 4 * W <= n:
        dst.store(i, src.load[width=W](i))
        dst.store(i + W, src.load[width=W](i + W))
        dst.store(i + 2 * W, src.load[width=W](i + 2 * W))
        dst.store(i + 3 * W, src.load[width=W](i + 3 * W))
        i += 4 * W
    while i + W <= n:
        dst.store(i, src.load[width=W](i))
        i += W
    while i < n:
        dst[i] = src[i]
        i += 1


@export("mcbor_copy_bytes")
def mcbor_copy_bytes(src_addr: Int, dst_addr: Int, size: Int) abi("C") -> Int:
    if size < 0 or src_addr == 0 or dst_addr == 0:
        return -3
    var src = BPtr(unsafe_from_address=src_addr)
    var dst = BPtr(unsafe_from_address=dst_addr)
    copy_bytes(src, dst, size)
    return size


@export("mcbor_encode")
def mcbor_encode(
    tape_addr: Int,
    count: Int,
    payload_base_addr: Int,
    dst_addr: Int,
    capacity: Int,
) abi("C") -> Int:
    if count < 0 or capacity < 0:
        return -3
    if tape_addr == 0 or dst_addr == 0:
        return -3
    var tape = U64Ptr(unsafe_from_address=tape_addr)
    var dst = BPtr(unsafe_from_address=dst_addr)
    var pos = 0
    for i in range(count):
        var base = i * 4
        var kind = Int(tape[base])
        var aggregate_payload = kind >= 16
        if aggregate_payload:
            kind -= 16
        var arg = tape[base + 1]
        var payload_addr = Int(tape[base + 2])
        var payload_len = Int(tape[base + 3])
        if payload_len < 0:
            return -3
        var needed: Int
        if kind <= 6:
            needed = head_size(arg)
            if kind == 2 or kind == 3:
                needed += payload_len
        elif kind == 7:
            needed = 1 if arg < 24 else 2
        elif kind == 8:
            needed = payload_len
        elif kind == 9:
            needed = 1
        else:
            return -2
        if needed < 0 or pos > capacity or needed > capacity - pos:
            return -1
        if kind <= 6:
            pos = write_head(dst, pos, kind, arg)
            if kind == 2 or kind == 3:
                if payload_len > 0:
                    var src_addr = payload_addr
                    if aggregate_payload:
                        if payload_base_addr == 0:
                            return -3
                        src_addr = payload_base_addr + payload_addr
                    if src_addr == 0:
                        return -3
                    var src = BPtr(unsafe_from_address=src_addr)
                    var target = BPtr(unsafe_from_address=dst_addr + pos)
                    copy_bytes(src, target, payload_len)
                    pos += payload_len
        elif kind == 7:
            if arg < 24:
                dst[pos] = UInt8(0xE0 | Int(arg))
                pos += 1
            else:
                dst[pos] = UInt8(0xF8)
                dst[pos + 1] = UInt8(arg)
                pos += 2
        elif kind == 8:
            if payload_len > 0:
                var src_addr = payload_addr
                if aggregate_payload:
                    if payload_base_addr == 0:
                        return -3
                    src_addr = payload_base_addr + payload_addr
                if src_addr == 0:
                    return -3
                var src = BPtr(unsafe_from_address=src_addr)
                var target = BPtr(unsafe_from_address=dst_addr + pos)
                copy_bytes(src, target, payload_len)
                pos += payload_len
        else:
            dst[pos] = UInt8(0xFF)
            pos += 1
    return pos


@export("mcbor_uint_array_size")
def mcbor_uint_array_size(values_addr: Int, count: Int) abi("C") -> Int:
    if count < 0 or values_addr == 0:
        return -3
    var values = U64Ptr(unsafe_from_address=values_addr)
    var size = head_size(UInt64(count))
    for i in range(count):
        size += head_size(values[i])
    return size


@export("mcbor_encode_uint_array")
def mcbor_encode_uint_array(
    values_addr: Int,
    count: Int,
    dst_addr: Int,
    capacity: Int,
) abi("C") -> Int:
    if count < 0 or capacity < 0:
        return -3
    if values_addr == 0 or dst_addr == 0:
        return -3
    var values = U64Ptr(unsafe_from_address=values_addr)
    var dst = BPtr(unsafe_from_address=dst_addr)
    var array_head_size = head_size(UInt64(count))
    if array_head_size > capacity:
        return -1
    var pos = write_head(dst, 0, 4, UInt64(count))
    for i in range(count):
        var needed = head_size(values[i])
        if pos + needed > capacity:
            return -1
        pos = write_head(dst, pos, 0, values[i])
    return pos


@export("mcbor_decode_uint_array")
def mcbor_decode_uint_array(
    src_addr: Int,
    size: Int,
    values_addr: Int,
    expected_count: Int,
) abi("C") -> Int:
    if size <= 0 or expected_count < 0:
        return -1
    if src_addr == 0 or values_addr == 0:
        return -3
    var src = BPtr(unsafe_from_address=src_addr)
    var values = U64Ptr(unsafe_from_address=values_addr)

    var first = Int(src[0])
    if first >> 5 != 4:
        return -2
    var ai = first & 31
    var pos = 1
    var count = UInt64(0)
    if ai < 24:
        count = UInt64(ai)
    elif ai == 24:
        if pos + 1 > size:
            return -1
        count = UInt64(src[pos])
        pos += 1
    elif ai == 25:
        if pos + 2 > size:
            return -1
        count = (UInt64(src[pos]) << 8) | UInt64(src[pos + 1])
        pos += 2
    elif ai == 26:
        if pos + 4 > size:
            return -1
        count = (
            (UInt64(src[pos]) << 24)
            | (UInt64(src[pos + 1]) << 16)
            | (UInt64(src[pos + 2]) << 8)
            | UInt64(src[pos + 3])
        )
        pos += 4
    elif ai == 27:
        if pos + 8 > size:
            return -1
        for j in range(8):
            count = (count << 8) | UInt64(src[pos + j])
        pos += 8
    else:
        return -2
    if count != UInt64(expected_count):
        return -2

    for i in range(expected_count):
        if pos >= size:
            return -1
        first = Int(src[pos])
        pos += 1
        if first >> 5 != 0:
            return -2
        ai = first & 31
        var value = UInt64(0)
        if ai < 24:
            value = UInt64(ai)
        elif ai == 24:
            if pos + 1 > size:
                return -1
            value = UInt64(src[pos])
            pos += 1
        elif ai == 25:
            if pos + 2 > size:
                return -1
            value = (UInt64(src[pos]) << 8) | UInt64(src[pos + 1])
            pos += 2
        elif ai == 26:
            if pos + 4 > size:
                return -1
            value = (
                (UInt64(src[pos]) << 24)
                | (UInt64(src[pos + 1]) << 16)
                | (UInt64(src[pos + 2]) << 8)
                | UInt64(src[pos + 3])
            )
            pos += 4
        elif ai == 27:
            if pos + 8 > size:
                return -1
            for j in range(8):
                value = (value << 8) | UInt64(src[pos + j])
            pos += 8
        else:
            return -2
        values[i] = value
    return pos


@export("mcbor_scan")
def mcbor_scan(
    src_addr: Int,
    size: Int,
    tape_addr: Int,
    capacity: Int,
    allow_indefinite: Int,
) abi("C") -> Int:
    if size < 0 or capacity < 0:
        return -3
    if src_addr == 0 or tape_addr == 0:
        return -3
    var src = BPtr(unsafe_from_address=src_addr)
    var tape = U64Ptr(unsafe_from_address=tape_addr)
    var pos = 0
    var count = 0
    while pos < size:
        var start = pos
        var first = Int(src[pos])
        pos += 1
        var major = first >> 5
        var ai = first & 31
        var kind = major
        var arg = UInt64(0)
        var payload_offset = 0
        var payload_len = 0

        if ai < 24:
            arg = UInt64(ai)
        elif ai == 24:
            if pos + 1 > size:
                return -1
            arg = UInt64(src[pos])
            pos += 1
        elif ai == 25:
            if pos + 2 > size:
                return -1
            arg = (UInt64(src[pos]) << 8) | UInt64(src[pos + 1])
            pos += 2
        elif ai == 26:
            if pos + 4 > size:
                return -1
            arg = (
                (UInt64(src[pos]) << 24)
                | (UInt64(src[pos + 1]) << 16)
                | (UInt64(src[pos + 2]) << 8)
                | UInt64(src[pos + 3])
            )
            pos += 4
        elif ai == 27:
            if pos + 8 > size:
                return -1
            for j in range(8):
                arg = (arg << 8) | UInt64(src[pos + j])
            pos += 8
        elif ai == 31:
            if major == 7:
                kind = 11
            elif major >= 2 and major <= 5 and allow_indefinite != 0:
                arg = UInt64(0xFFFFFFFFFFFFFFFF)
            else:
                return -2
        else:
            return -2

        if major == 2 or major == 3:
            if ai != 31:
                if arg > UInt64(size - pos):
                    return -1
                payload_offset = pos
                payload_len = Int(arg)
                pos += payload_len
        elif major == 7:
            if ai < 24:
                kind = 7
                arg = UInt64(ai)
            elif ai == 24:
                if arg >= 24 and arg < 32:
                    return -2
                kind = 7
            elif ai == 25:
                kind = 8
            elif ai == 26:
                kind = 9
            elif ai == 27:
                kind = 10
            elif ai == 31:
                kind = 11
            else:
                return -2

        if count < capacity:
            var base = count * 4
            tape[base] = UInt64(kind)
            tape[base + 1] = arg
            tape[base + 2] = UInt64(start)
            tape[base + 3] = UInt64(payload_offset)
        count += 1
        _ = start
    return count
