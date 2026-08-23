from __future__ import annotations

import array
import datetime as _datetime
import ipaddress
import math
import re
import struct
import uuid
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from typing import Any

from . import _lib


class CBORError(Exception):
    pass


class CBOREncodeError(CBORError):
    pass


class CBOREncodeTypeError(CBOREncodeError, TypeError):
    pass


class CBOREncodeValueError(CBOREncodeError, ValueError):
    pass


class CBORDecodeError(CBORError):
    pass


class CBORDecodeEOF(CBORDecodeError, EOFError):
    pass


class _UndefinedType:
    __slots__ = ()

    def __repr__(self) -> str:
        return "undefined"

    def __reduce__(self):
        return "undefined"


undefined = _UndefinedType()


class CBORTag:
    __slots__ = ("tag", "value")

    def __init__(self, tag: int, value: Any):
        if not isinstance(tag, int) or isinstance(tag, bool) or not 0 <= tag < 2**64:
            raise TypeError("tag must be an unsigned 64-bit integer")
        self.tag = tag
        self.value = value

    def __repr__(self) -> str:
        return f"CBORTag({self.tag!r}, {self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CBORTag) and (self.tag, self.value) == (
            other.tag,
            other.value,
        )

    def __hash__(self) -> int:
        return hash((self.tag, self.value))


class CBORSimpleValue:
    __slots__ = ("value",)

    def __init__(self, value: int):
        if not isinstance(value, int) or isinstance(value, bool) or not (
            0 <= value <= 23 or 32 <= value <= 255
        ):
            raise ValueError("simple value out of range (0..23, 32..255)")
        self.value = value

    def __repr__(self) -> str:
        return f"CBORSimpleValue({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CBORSimpleValue) and self.value == other.value

    def __hash__(self) -> int:
        return hash((CBORSimpleValue, self.value))


class frozendict(Mapping):
    __slots__ = ("_items", "_dict", "_hash")

    def __init__(self, *args, **kwargs):
        self._dict = dict(*args, **kwargs)
        self._items = tuple(self._dict.items())
        self._hash = None

    def __getitem__(self, key):
        return self._dict[key]

    def __iter__(self):
        return iter(self._dict)

    def __len__(self):
        return len(self._dict)

    def __hash__(self):
        if self._hash is None:
            self._hash = hash(frozenset(self._items))
        return self._hash

    def __repr__(self):
        return f"frozendict({self._dict!r})"


_MAX_U64 = 2**64 - 1
_INDEFINITE = _MAX_U64


def _head_size(value: int) -> int:
    if value < 24:
        return 1
    if value <= 0xFF:
        return 2
    if value <= 0xFFFF:
        return 3
    if value <= 0xFFFFFFFF:
        return 5
    return 9


def _bytes_head(length: int) -> bytes:
    if length < 24:
        return bytes((0x40 | length,))
    if length <= 0xFF:
        return bytes((0x58, length))
    if length <= 0xFFFF:
        return b"\x59" + length.to_bytes(2, "big")
    if length <= 0xFFFFFFFF:
        return b"\x5a" + length.to_bytes(4, "big")
    return b"\x5b" + length.to_bytes(8, "big")


def _float_bytes(value: float, canonical: bool) -> bytes:
    if math.isnan(value):
        return b"\xf9\x7e\x00"
    if canonical or math.isinf(value):
        original = struct.pack(">d", value)
        try:
            half = struct.pack(">e", value)
            if struct.pack(">d", struct.unpack(">e", half)[0]) == original:
                return b"\xf9" + half
        except (OverflowError, struct.error):
            pass
        single = struct.pack(">f", value)
        if struct.pack(">d", struct.unpack(">f", single)[0]) == original:
            return b"\xfa" + single
    return b"\xfb" + struct.pack(">d", value)


class _Builder:
    def __init__(
        self,
        *,
        datetime_as_timestamp=False,
        timezone=None,
        encoders=None,
        default=None,
        canonical=False,
        date_as_datetime=False,
        indefinite_containers=False,
    ):
        self.datetime_as_timestamp = datetime_as_timestamp
        self.timezone = timezone
        self.encoders = encoders or {}
        self.default = default
        self.canonical = canonical
        self.date_as_datetime = date_as_datetime
        self.indefinite = indefinite_containers
        self.tape: list[int] = []
        self.keepalive: list[object] = []
        self.payloads: list[bytes] = []
        self.payload_size = 0
        self.size = 0

    def _record(
        self,
        kind: int,
        arg: int = 0,
        payload: bytes | memoryview | None = None,
        *,
        aggregate: bool = False,
        pointer: int | None = None,
    ):
        length = 0
        if payload is not None:
            length = len(payload)
            if aggregate:
                pointer = self.payload_size
                self.payload_size += length
                self.payloads.append(payload)
                kind += 16
            else:
                self.keepalive.append(payload)
                if length and pointer is None:
                    pointer = _lib.bytes_addr(payload)
        self.tape.extend((kind, arg, pointer or 0, length))
        if kind >= 16:
            kind -= 16
        if kind <= 6:
            self.size += _head_size(arg)
            if kind in (2, 3):
                self.size += length
        elif kind == 7:
            self.size += 1 if arg < 24 else 2
        elif kind == 8:
            self.size += length
        else:
            self.size += 1

    def _head_record(self, kind: int, arg: int = 0):
        self.tape.extend((kind, arg, 0, 0))
        if kind <= 6:
            self.size += _head_size(arg)
        elif kind == 7:
            self.size += 1 if arg < 24 else 2
        else:
            self.size += 1

    def _aggregate_record(self, kind: int, arg: int, payload: bytes):
        length = len(payload)
        self.tape.extend((kind + 16, arg, self.payload_size, length))
        self.payload_size += length
        self.payloads.append(payload)
        if kind == 2 or kind == 3:
            self.size += _head_size(arg) + length
        else:
            self.size += length

    def _raw(self, value: bytes):
        self._aggregate_record(8, 0, value)

    def encode(self, obj):
        self.visit(obj)

    def encode_to_bytes(self, obj) -> bytes:
        return _encode(obj, self.options())

    def options(self):
        return dict(
            datetime_as_timestamp=self.datetime_as_timestamp,
            timezone=self.timezone,
            encoders=self.encoders,
            default=self.default,
            canonical=self.canonical,
            date_as_datetime=self.date_as_datetime,
            indefinite_containers=self.indefinite,
        )

    def _int(self, value: int):
        if 0 <= value <= _MAX_U64:
            self._head_record(0, value)
        elif -1 - _MAX_U64 <= value < 0:
            self._head_record(1, -1 - value)
        elif value >= 0:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            self._head_record(6, 2)
            self._aggregate_record(2, len(raw), raw)
        else:
            magnitude = -1 - value
            raw = magnitude.to_bytes((magnitude.bit_length() + 7) // 8, "big")
            self._head_record(6, 3)
            self._aggregate_record(2, len(raw), raw)

    def _datetime(self, value: _datetime.datetime):
        if value.tzinfo is None:
            if self.timezone is None:
                raise CBOREncodeValueError(
                    "naive datetime encountered and no default timezone has been set"
                )
            value = value.replace(tzinfo=self.timezone)
        if self.datetime_as_timestamp:
            self._head_record(6, 1)
            timestamp = value.timestamp()
            self.visit(int(timestamp) if timestamp.is_integer() else timestamp)
        else:
            self._head_record(6, 0)
            text = value.isoformat()
            if value.utcoffset() == _datetime.timedelta(0):
                text = text.replace("+00:00", "Z")
            self.visit(text)

    def _canonical_items(self, items):
        encoded = [(_encode(k, self.options()), k, v) for k, v in items]
        encoded.sort(key=lambda row: (len(row[0]), row[0]))
        return encoded

    def visit(self, obj):
        obj_type = type(obj)
        if obj is None:
            self._head_record(7, 22)
        elif obj is undefined:
            self._head_record(7, 23)
        elif obj is False:
            self._head_record(7, 20)
        elif obj is True:
            self._head_record(7, 21)
        elif obj_type is int:
            self._int(obj)
        elif obj_type is float:
            self._raw(_float_bytes(obj, self.canonical))
        elif obj_type is bytes:
            self._record(2, len(obj), obj)
        elif obj_type is bytearray or obj_type is memoryview:
            try:
                value = memoryview(obj).cast("B")
            except TypeError:
                value = memoryview(bytes(obj))
            if value.readonly:
                copied = value.tobytes()
                self._record(2, len(copied), copied)
            else:
                self._record(
                    2,
                    len(value),
                    value,
                    pointer=_lib.writable_buffer_addr(value) if value else 0,
                )
        elif obj_type is str:
            value = obj.encode("utf-8")
            self._aggregate_record(3, len(value), value)
        elif obj_type is list or obj_type is tuple:
            if self.indefinite and not self.canonical:
                self._raw(b"\x9f")
                for item in obj:
                    self.visit(item)
                self._head_record(9)
            else:
                self._head_record(4, len(obj))
                for item in obj:
                    self.visit(item)
        elif obj_type is dict:
            if self.indefinite and not self.canonical:
                self._raw(b"\xbf")
                for key, value in obj.items():
                    self.visit(key)
                    self.visit(value)
                self._head_record(9)
            else:
                self._head_record(5, len(obj))
                if self.canonical:
                    for key_bytes, _, value in self._canonical_items(obj.items()):
                        self._raw(key_bytes)
                        self.visit(value)
                else:
                    for key, value in obj.items():
                        self.visit(key)
                        self.visit(value)
        elif isinstance(obj, int):
            self._int(obj)
        elif isinstance(obj, float):
            self._raw(_float_bytes(obj, self.canonical))
        elif isinstance(obj, bytes):
            self._record(2, len(obj), obj)
        elif isinstance(obj, (bytearray, memoryview)):
            value = memoryview(obj)
            copied = value.tobytes()
            self._record(2, len(copied), copied)
        elif isinstance(obj, str):
            value = obj.encode("utf-8")
            self._aggregate_record(3, len(value), value)
        elif isinstance(obj, (list, tuple)):
            if self.indefinite and not self.canonical:
                self._raw(b"\x9f")
                for item in obj:
                    self.visit(item)
                self._head_record(9)
            else:
                self._head_record(4, len(obj))
                for item in obj:
                    self.visit(item)
        elif isinstance(obj, Mapping):
            if self.indefinite and not self.canonical:
                self._raw(b"\xbf")
                for key, value in obj.items():
                    self.visit(key)
                    self.visit(value)
                self._head_record(9)
            else:
                self._head_record(5, len(obj))
                if self.canonical:
                    for key_bytes, _, value in self._canonical_items(obj.items()):
                        self._raw(key_bytes)
                        self.visit(value)
                else:
                    for key, value in obj.items():
                        self.visit(key)
                        self.visit(value)
        elif isinstance(obj, (set, frozenset)):
            self._head_record(6, 258)
            values = list(obj)
            if self.canonical:
                encoded = sorted(
                    (_encode(x, self.options()) for x in values),
                    key=lambda x: (len(x), x),
                )
                self._head_record(4, len(encoded))
                for item in encoded:
                    self._raw(item)
            else:
                self._head_record(4, len(values))
                for item in values:
                    self.visit(item)
        elif isinstance(obj, CBORTag):
            self._head_record(6, obj.tag)
            self.visit(obj.value)
        elif isinstance(obj, CBORSimpleValue):
            self._head_record(7, obj.value)
        elif isinstance(obj, _datetime.datetime):
            self._datetime(obj)
        elif isinstance(obj, _datetime.date):
            if self.date_as_datetime:
                self._datetime(
                    _datetime.datetime.combine(obj, _datetime.time(), self.timezone)
                )
            else:
                self._head_record(
                    6, 100 if self.datetime_as_timestamp else 1004
                )
                if self.datetime_as_timestamp:
                    self.visit((obj - _datetime.date(1970, 1, 1)).days)
                else:
                    self.visit(obj.isoformat())
        elif isinstance(obj, Decimal):
            self._head_record(6, 4)
            sign, digits, exponent = obj.as_tuple()
            mantissa = int("".join(map(str, digits)) or "0")
            self.visit([exponent, -mantissa if sign else mantissa])
        elif isinstance(obj, Fraction):
            self._head_record(6, 30)
            self.visit([obj.numerator, obj.denominator])
        elif isinstance(obj, uuid.UUID):
            self._head_record(6, 37)
            self.visit(obj.bytes)
        elif isinstance(obj, re.Pattern):
            self._head_record(6, 35)
            self.visit(obj.pattern)
        elif isinstance(obj, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            self._head_record(6, 52 if obj.version == 4 else 54)
            self.visit(obj.packed)
        elif isinstance(obj, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            self._head_record(6, 52 if obj.version == 4 else 54)
            used = (obj.prefixlen + 7) // 8
            self.visit([obj.prefixlen, obj.network_address.packed[:used]])
        else:
            encoder = next(
                (fn for cls, fn in self.encoders.items() if isinstance(obj, cls)), None
            )
            if encoder is not None:
                encoder(self, obj)
            elif self.default is not None:
                self.default(self, obj)
            else:
                raise CBOREncodeTypeError(
                    f"cannot serialize type {type(obj).__name__}"
                )


def _encode(obj, options) -> bytes:
    if type(obj) is bytes and len(obj) >= 4 << 20:
        return _lib.encode_bytes_parallel(obj, _bytes_head(len(obj)))
    if (
        isinstance(obj, (list, tuple))
        and len(obj) >= 64
        and not options["indefinite_containers"]
        and all(type(value) is int for value in obj)
    ):
        try:
            values = array.array("Q", obj)
        except OverflowError:
            pass
        else:
            return _lib.encode_uint_array(values)
    flat_maps = _encode_flat_map_array(obj, options)
    if flat_maps is not None:
        return flat_maps
    builder = _Builder(**options)
    builder.visit(obj)
    tape = array.array("Q", builder.tape)
    payloads = b"".join(builder.payloads)
    return _lib.encode(tape, builder.size, payloads)


def _encode_flat_map_array(obj, options) -> bytes | None:
    if (
        type(obj) not in (list, tuple)
        or len(obj) < 256
        or options["indefinite_containers"]
        or options["canonical"]
        or options["encoders"]
        or options["default"] is not None
    ):
        return None

    tape = [4, len(obj), 0, 0]
    payloads = []
    payload_size = 0
    size = _head_size(len(obj))
    key_cache = {}
    extend = tape.extend
    append_payload = payloads.append
    head_size = _head_size

    for item in obj:
        if type(item) is not dict:
            return None
        extend((5, len(item), 0, 0))
        size += head_size(len(item))
        for key, value in item.items():
            key_type = type(key)
            if key_type is str:
                encoded = key_cache.get(key)
                if encoded is None:
                    encoded = key.encode("utf-8")
                    key_cache[key] = encoded
                length = len(encoded)
                extend((19, length, payload_size, length))
                append_payload(encoded)
                payload_size += length
                size += head_size(length) + length
            elif key_type is int:
                if 0 <= key <= _MAX_U64:
                    extend((0, key, 0, 0))
                    size += head_size(key)
                elif -1 - _MAX_U64 <= key < 0:
                    arg = -1 - key
                    extend((1, arg, 0, 0))
                    size += head_size(arg)
                else:
                    return None
            elif key is False:
                extend((7, 20, 0, 0))
                size += 1
            elif key is True:
                extend((7, 21, 0, 0))
                size += 1
            elif key is None:
                extend((7, 22, 0, 0))
                size += 1
            elif key_type is bytes:
                length = len(key)
                extend((18, length, payload_size, length))
                append_payload(key)
                payload_size += length
                size += head_size(length) + length
            else:
                return None

            value_type = type(value)
            if value_type is str:
                encoded = value.encode("utf-8")
                length = len(encoded)
                extend((19, length, payload_size, length))
                append_payload(encoded)
                payload_size += length
                size += head_size(length) + length
            elif value_type is int:
                if 0 <= value <= _MAX_U64:
                    extend((0, value, 0, 0))
                    size += head_size(value)
                elif -1 - _MAX_U64 <= value < 0:
                    arg = -1 - value
                    extend((1, arg, 0, 0))
                    size += head_size(arg)
                else:
                    return None
            elif value is False:
                extend((7, 20, 0, 0))
                size += 1
            elif value is True:
                extend((7, 21, 0, 0))
                size += 1
            elif value is None:
                extend((7, 22, 0, 0))
                size += 1
            elif value is undefined:
                extend((7, 23, 0, 0))
                size += 1
            elif value_type is bytes:
                length = len(value)
                extend((18, length, payload_size, length))
                append_payload(value)
                payload_size += length
                size += head_size(length) + length
            elif value_type is float:
                encoded = _float_bytes(value, False)
                length = len(encoded)
                extend((24, 0, payload_size, length))
                append_payload(encoded)
                payload_size += length
                size += length
            else:
                return None

    return _lib.encode(array.array("Q", tape), size, b"".join(payloads))


def dumps(
    obj,
    *,
    datetime_as_timestamp=False,
    timezone=None,
    value_sharing=False,
    encoders=None,
    default=None,
    canonical=False,
    date_as_datetime=False,
    string_referencing=False,
    indefinite_containers=False,
):
    if value_sharing or string_referencing:
        raise NotImplementedError(
            "value_sharing and string_referencing are not in the covered subset"
        )
    return _encode(
        obj,
        dict(
            datetime_as_timestamp=datetime_as_timestamp,
            timezone=timezone,
            encoders=encoders,
            default=default,
            canonical=canonical,
            date_as_datetime=date_as_datetime,
            indefinite_containers=indefinite_containers,
        ),
    )


def dump(obj, fp, **kwargs):
    fp.write(dumps(obj, **kwargs))


class _Parser:
    def __init__(
        self,
        data,
        tape,
        *,
        tag_hook,
        object_hook,
        semantic_decoders,
        str_errors,
        max_depth,
        allow_duplicate_keys,
        immutable,
    ):
        self.data = data
        self.tape = tape
        self.count = len(tape) // 4
        self.index = 0
        self.tag_hook = tag_hook
        self.object_hook = object_hook
        self.semantic_decoders = semantic_decoders or {}
        self.str_errors = str_errors
        self.max_depth = max_depth
        self.allow_duplicate_keys = allow_duplicate_keys
        self.immutable = immutable
        self.data_is_bytes = isinstance(data, bytes)
        self.text_cache: dict[bytes, str] = {}

    def _token(self):
        if self.index >= self.count:
            raise CBORDecodeEOF("premature end of stream")
        base = self.index * 4
        self.index += 1
        return (
            self.tape[base],
            self.tape[base + 1],
            self.tape[base + 2],
            self.tape[base + 3],
        )

    def _peek_kind(self):
        if self.index >= self.count:
            raise CBORDecodeEOF("premature end of stream")
        return self.tape[self.index * 4]

    def parse(self, depth=0, immutable=None):
        if depth > self.max_depth:
            raise CBORDecodeError("maximum nesting depth exceeded")
        if immutable is None:
            immutable = self.immutable
        if self.index >= self.count:
            raise CBORDecodeEOF("premature end of stream")
        base = self.index * 4
        self.index += 1
        kind = self.tape[base]
        arg = self.tape[base + 1]
        payload_offset = self.tape[base + 3]
        if kind == 0:
            return arg
        if kind == 1:
            return -1 - arg
        if kind in (2, 3):
            if arg == _INDEFINITE:
                parts = []
                while self._peek_kind() != 11:
                    part_kind = self._peek_kind()
                    if part_kind != kind:
                        raise CBORDecodeError("invalid indefinite string chunk")
                    parts.append(self.parse(depth + 1, immutable))
                self._token()
                return b"".join(parts) if kind == 2 else "".join(parts)
            raw = self.data[payload_offset : payload_offset + arg]
            if not self.data_is_bytes:
                raw = raw.tobytes()
            if kind == 2:
                return raw
            if immutable:
                cached = self.text_cache.get(raw)
                if cached is not None:
                    return cached
                decoded = raw.decode("utf-8", self.str_errors)
                if len(self.text_cache) < 256:
                    self.text_cache[raw] = decoded
                return decoded
            return raw.decode("utf-8", self.str_errors)
        if kind == 4:
            if arg == _INDEFINITE:
                values = []
                while self._peek_kind() != 11:
                    values.append(self.parse(depth + 1, immutable))
                self._token()
            else:
                values = [
                    self.parse(depth + 1, immutable) for _ in range(arg)
                ]
            return tuple(values) if immutable else values
        if kind == 5:
            values = {}
            indefinite = arg == _INDEFINITE
            remaining = None if indefinite else arg
            while (self._peek_kind() != 11) if indefinite else remaining:
                key = self.parse(depth + 1, True)
                if not self.allow_duplicate_keys and key in values:
                    raise CBORDecodeError(f"duplicate map key {key!r}")
                values[key] = self.parse(depth + 1, immutable)
                if remaining is not None:
                    remaining -= 1
            if indefinite:
                self._token()
            result = frozendict(values) if immutable else values
            return self.object_hook(result, immutable) if self.object_hook else result
        if kind == 6:
            value = self.parse(depth + 1, immutable)
            return self._tag(arg, value, immutable)
        if kind == 7:
            if arg == 20:
                return False
            if arg == 21:
                return True
            if arg == 22:
                return None
            if arg == 23:
                return undefined
            return CBORSimpleValue(arg)
        if kind == 8:
            return struct.unpack(">e", arg.to_bytes(2, "big"))[0]
        if kind == 9:
            return struct.unpack(">f", arg.to_bytes(4, "big"))[0]
        if kind == 10:
            return struct.unpack(">d", arg.to_bytes(8, "big"))[0]
        raise CBORDecodeError("unexpected break marker")

    def _tag(self, tag, value, immutable):
        if tag in self.semantic_decoders:
            return self.semantic_decoders[tag](value, immutable)
        try:
            if tag == 0:
                return _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if tag == 1:
                return _datetime.datetime.fromtimestamp(
                    value, tz=_datetime.timezone.utc
                )
            if tag == 2:
                return int.from_bytes(value, "big")
            if tag == 3:
                return -1 - int.from_bytes(value, "big")
            if tag == 4:
                return Decimal(value[1]).scaleb(value[0])
            if tag == 5:
                return Decimal(value[1]) * (Decimal(2) ** value[0])
            if tag == 30:
                return Fraction(*value)
            if tag == 35:
                return re.compile(value)
            if tag == 37:
                return uuid.UUID(bytes=value)
            if tag == 100:
                return _datetime.date(1970, 1, 1) + _datetime.timedelta(days=value)
            if tag == 1004:
                return _datetime.date.fromisoformat(value)
            if tag == 258:
                return frozenset(value) if immutable else set(value)
            if tag in (52, 54):
                version_size = 4 if tag == 52 else 16
                if isinstance(value, bytes):
                    if len(value) != version_size:
                        raise ValueError
                    return ipaddress.ip_address(value)
                prefix, packed = value
                if not 0 <= prefix <= version_size * 8:
                    raise ValueError
                return ipaddress.ip_network(
                    (ipaddress.ip_address(packed.ljust(version_size, b"\x00")), prefix)
                )
            if tag == 260:
                return ipaddress.ip_address(value)
            if tag == 261:
                prefix, packed = next(iter(value.items()))
                return ipaddress.ip_network((ipaddress.ip_address(packed), prefix))
            if tag == 55799:
                return value
        except (TypeError, ValueError, OverflowError, IndexError) as exc:
            raise CBORDecodeError(f"invalid value for semantic tag {tag}") from exc
        tagged = CBORTag(tag, value)
        return self.tag_hook(tagged, immutable) if self.tag_hook else tagged


def _loads_one(
    data,
    *,
    tag_hook=None,
    object_hook=None,
    semantic_decoders=None,
    str_errors="strict",
    max_depth=400,
    allow_indefinite=True,
    allow_duplicate_keys=True,
    immutable=False,
):
    try:
        if isinstance(data, bytes):
            raw = data
        else:
            raw = memoryview(data).cast("B")
    except (TypeError, ValueError) as exc:
        raise TypeError("a bytes-like object is required") from exc
    if not isinstance(raw, bytes) and raw.readonly:
        raw = raw.tobytes()
    if not raw:
        raise CBORDecodeEOF("premature end of stream")
    array_info = _definite_array_info(raw)
    if (
        array_info is not None
        and 64 <= array_info[0] <= len(raw)
        and array_info[1] < len(raw)
        and raw[array_info[1]] >> 5 == 0
        and max_depth >= 1
    ):
        count = array_info[0]
        decoded = _lib.decode_uint_array(raw, count)
        if decoded is not None:
            values, consumed = decoded
            return (tuple(values) if immutable else values), consumed
    try:
        tape = _lib.scan(raw, allow_indefinite)
    except EOFError as exc:
        raise CBORDecodeEOF("premature end of stream") from exc
    except ValueError as exc:
        raise CBORDecodeError(str(exc)) from exc
    flat_maps = _decode_flat_map_array(
        raw,
        tape,
        tag_hook=tag_hook,
        object_hook=object_hook,
        semantic_decoders=semantic_decoders,
        str_errors=str_errors,
        max_depth=max_depth,
        allow_duplicate_keys=allow_duplicate_keys,
        immutable=immutable,
    )
    if flat_maps is not None:
        return flat_maps
    parser = _Parser(
        raw,
        tape,
        tag_hook=tag_hook,
        object_hook=object_hook,
        semantic_decoders=semantic_decoders,
        str_errors=str_errors,
        max_depth=max_depth,
        allow_duplicate_keys=allow_duplicate_keys,
        immutable=immutable,
    )
    value = parser.parse()
    consumed = (
        tape[parser.index * 4 + 2] if parser.index < parser.count else len(raw)
    )
    return value, consumed


def _decode_flat_map_array(
    data,
    tape,
    *,
    tag_hook,
    object_hook,
    semantic_decoders,
    str_errors,
    max_depth,
    allow_duplicate_keys,
    immutable,
):
    if (
        len(tape) < 4
        or tape[0] != 4
        or tape[1] < 256
        or tape[1] == _INDEFINITE
        or tag_hook is not None
        or object_hook is not None
        or semantic_decoders
        or immutable
        or max_depth < 2
    ):
        return None

    count = tape[1]
    token_count = len(tape) // 4
    index = 1
    result = []
    append_result = result.append
    text_cache = {}
    cache_get = text_cache.get
    data_is_bytes = isinstance(data, bytes)

    for _ in range(count):
        if index >= token_count:
            return None
        base = index * 4
        if tape[base] != 5 or tape[base + 1] == _INDEFINITE:
            return None
        pair_count = tape[base + 1]
        index += 1
        item = {}
        for _ in range(pair_count):
            if index + 1 >= token_count:
                return None
            base = index * 4
            index += 1
            kind = tape[base]
            arg = tape[base + 1]
            if kind == 0:
                key = arg
            elif kind == 1:
                key = -1 - arg
            elif kind == 2 or kind == 3:
                offset = tape[base + 3]
                raw = data[offset : offset + arg]
                if not data_is_bytes:
                    raw = raw.tobytes()
                if kind == 2:
                    key = raw
                else:
                    key = cache_get(raw)
                    if key is None:
                        key = raw.decode("utf-8", str_errors)
                        if len(text_cache) < 256:
                            text_cache[raw] = key
            elif kind == 7:
                if arg == 20:
                    key = False
                elif arg == 21:
                    key = True
                elif arg == 22:
                    key = None
                elif arg == 23:
                    key = undefined
                else:
                    key = CBORSimpleValue(arg)
            else:
                return None

            if not allow_duplicate_keys and key in item:
                raise CBORDecodeError(f"duplicate map key {key!r}")

            base = index * 4
            index += 1
            kind = tape[base]
            arg = tape[base + 1]
            if kind == 0:
                value = arg
            elif kind == 1:
                value = -1 - arg
            elif kind == 2 or kind == 3:
                offset = tape[base + 3]
                raw = data[offset : offset + arg]
                if not data_is_bytes:
                    raw = raw.tobytes()
                value = raw if kind == 2 else raw.decode("utf-8", str_errors)
            elif kind == 7:
                if arg == 20:
                    value = False
                elif arg == 21:
                    value = True
                elif arg == 22:
                    value = None
                elif arg == 23:
                    value = undefined
                else:
                    value = CBORSimpleValue(arg)
            elif kind == 8:
                value = struct.unpack(">e", arg.to_bytes(2, "big"))[0]
            elif kind == 9:
                value = struct.unpack(">f", arg.to_bytes(4, "big"))[0]
            elif kind == 10:
                value = struct.unpack(">d", arg.to_bytes(8, "big"))[0]
            else:
                return None
            item[key] = value
        append_result(item)

    consumed = tape[index * 4 + 2] if index < token_count else len(data)
    return result, consumed


def _definite_array_info(
    data: bytes | memoryview,
) -> tuple[int, int] | None:
    first = data[0]
    if first >> 5 != 4:
        return None
    ai = first & 31
    if ai < 24:
        return ai, 1
    widths = {24: 1, 25: 2, 26: 4, 27: 8}
    width = widths.get(ai)
    if width is None or len(data) < width + 1:
        return None
    return int.from_bytes(data[1 : width + 1], "big"), width + 1


def loads(
    data,
    *,
    tag_hook=None,
    object_hook=None,
    semantic_decoders=None,
    str_errors="strict",
    max_depth=400,
    allow_indefinite=True,
    allow_duplicate_keys=True,
    immutable=False,
):
    value, consumed = _loads_one(
        data,
        tag_hook=tag_hook,
        object_hook=object_hook,
        semantic_decoders=semantic_decoders,
        str_errors=str_errors,
        max_depth=max_depth,
        allow_indefinite=allow_indefinite,
        allow_duplicate_keys=allow_duplicate_keys,
        immutable=immutable,
    )
    try:
        size = memoryview(data).nbytes
    except TypeError:
        size = len(data)
    if consumed != size:
        raise CBORDecodeError("extra data after first CBOR object")
    return value


def load(fp, *, read_size=4096, **kwargs):
    start = fp.tell() if hasattr(fp, "tell") else None
    data = fp.read()
    value, consumed = _loads_one(data, **kwargs)
    if start is not None and hasattr(fp, "seek"):
        fp.seek(start + consumed)
    return value


class CBOREncoder:
    def __init__(self, fp, **kwargs):
        self.fp = fp
        self.options = kwargs

    def encode(self, obj):
        dump(obj, self.fp, **self.options)

    def encode_to_bytes(self, obj):
        return dumps(obj, **self.options)


class CBORDecoder:
    def __init__(self, fp, *, read_size=4096, **kwargs):
        self.fp = fp
        self.options = kwargs
        self.read_size = read_size

    def decode(self):
        return load(self.fp, read_size=self.read_size, **self.options)


def shareable_encoder(wraps):
    return wraps


def shareable_decoder(func):
    return func


__all__ = [
    "CBORDecodeEOF",
    "CBORDecodeError",
    "CBORDecoder",
    "CBOREncodeError",
    "CBOREncodeTypeError",
    "CBOREncodeValueError",
    "CBOREncoder",
    "CBORError",
    "CBORSimpleValue",
    "CBORTag",
    "dump",
    "dumps",
    "frozendict",
    "load",
    "loads",
    "shareable_decoder",
    "shareable_encoder",
    "undefined",
]
