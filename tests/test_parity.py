from __future__ import annotations

import datetime
import decimal
import fractions
import io
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import uuid

import numpy as np
import pytest

import cbor2


@pytest.fixture(scope="session")
def upstream_hex():
    script = r"""
import cbor2, datetime, decimal, fractions, ipaddress, json, re, uuid
samples = {
    "none": None,
    "false": False,
    "true": True,
    "undefined": cbor2.undefined,
    "u23": 23,
    "u24": 24,
    "u65536": 65536,
    "n24": -24,
    "n25": -25,
    "bigpos": 2**80,
    "bigneg": -(2**80),
    "float": 1.5,
    "nan": float("nan"),
    "bytes": b"\x00\x01\xff",
    "text": "IETF \u20ac",
    "array": [1, "two", None],
    "tuple": (1, 2),
    "map": {"a": 1, 2: False},
    "tag": cbor2.CBORTag(42, "value"),
    "simple": cbor2.CBORSimpleValue(19),
    "date": datetime.date(2020, 1, 2),
    "datetime": datetime.datetime(2020, 1, 2, tzinfo=datetime.timezone.utc),
    "decimal": decimal.Decimal("12.34"),
    "fraction": fractions.Fraction(2, 3),
    "uuid": uuid.UUID(int=1),
    "regex": re.compile("a+"),
    "ip": ipaddress.ip_address("127.0.0.1"),
}
result = {}
for name, value in samples.items():
    result[name] = {
        "normal": cbor2.dumps(value).hex(),
        "canonical": cbor2.dumps(value, canonical=True).hex(),
    }
result["indefinite"] = {
    "normal": cbor2.dumps(
        [1, {"x": [2, 3]}], indefinite_containers=True
    ).hex()
}
result["canonical_map"] = {
    "normal": cbor2.dumps(
        {"aa": 1, 10: 2, "b": 3, -1: 4}, canonical=True
    ).hex()
}
print(json.dumps(result))
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd="/tmp",
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


SAMPLES = {
    "none": None,
    "false": False,
    "true": True,
    "undefined": cbor2.undefined,
    "u23": 23,
    "u24": 24,
    "u65536": 65536,
    "n24": -24,
    "n25": -25,
    "bigpos": 2**80,
    "bigneg": -(2**80),
    "float": 1.5,
    "nan": float("nan"),
    "bytes": b"\x00\x01\xff",
    "text": "IETF \u20ac",
    "array": [1, "two", None],
    "tuple": (1, 2),
    "map": {"a": 1, 2: False},
    "tag": cbor2.CBORTag(42, "value"),
    "simple": cbor2.CBORSimpleValue(19),
    "date": datetime.date(2020, 1, 2),
    "datetime": datetime.datetime(2020, 1, 2, tzinfo=datetime.timezone.utc),
    "decimal": decimal.Decimal("12.34"),
    "fraction": fractions.Fraction(2, 3),
    "uuid": uuid.UUID(int=1),
    "regex": re.compile("a+"),
    "ip": ipaddress.ip_address("127.0.0.1"),
}


@pytest.mark.parametrize("name", SAMPLES)
def test_default_encoding_matches_upstream(name, upstream_hex):
    assert cbor2.dumps(SAMPLES[name]).hex() == upstream_hex[name]["normal"]


@pytest.mark.parametrize("name", SAMPLES)
def test_canonical_encoding_matches_upstream(name, upstream_hex):
    assert (
        cbor2.dumps(SAMPLES[name], canonical=True).hex()
        == upstream_hex[name]["canonical"]
    )


def test_canonical_map_order_matches_upstream(upstream_hex):
    value = {"aa": 1, 10: 2, "b": 3, -1: 4}
    assert (
        cbor2.dumps(value, canonical=True).hex()
        == upstream_hex["canonical_map"]["normal"]
    )


def test_indefinite_container_encoding_matches_upstream(upstream_hex):
    value = [1, {"x": [2, 3]}]
    assert (
        cbor2.dumps(value, indefinite_containers=True).hex()
        == upstream_hex["indefinite"]["normal"]
    )


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        ("00", 0),
        ("1818", 24),
        ("1903e8", 1000),
        ("1a000f4240", 1000000),
        ("1b000000e8d4a51000", 1000000000000),
        ("20", -1),
        ("3903e7", -1000),
        ("43010203", b"\x01\x02\x03"),
        ("6449455446", "IETF"),
        ("83010203", [1, 2, 3]),
        ("a201020304", {1: 2, 3: 4}),
        ("f4", False),
        ("f5", True),
        ("f6", None),
        ("f93e00", 1.5),
        ("fa47c35000", 100000.0),
        ("fb3ff199999999999a", 1.1),
    ],
)
def test_rfc8949_vectors(encoded, expected):
    actual = cbor2.loads(bytes.fromhex(encoded))
    assert actual == expected


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        ("9f018202039f0405ffff", [1, [2, 3], [4, 5]]),
        ("5f42010243030405ff", b"\x01\x02\x03\x04\x05"),
        ("7f657374726561646d696e67ff", "streaming"),
        ("bf61610161629f0203ffff", {"a": 1, "b": [2, 3]}),
    ],
)
def test_indefinite_decoding(encoded, expected):
    assert cbor2.loads(bytes.fromhex(encoded)) == expected


def test_semantic_types_round_trip():
    values = [
        datetime.date(2024, 2, 29),
        datetime.datetime(2024, 2, 29, 12, 30, tzinfo=datetime.timezone.utc),
        decimal.Decimal("-123.4500"),
        fractions.Fraction(355, 113),
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        ipaddress.ip_address("2001:db8::1"),
        ipaddress.ip_network("10.0.0.0/8"),
        re.compile(r"a+\s"),
        {1, 2, 3},
        frozenset({"a", "b"}),
    ]
    for value in values:
        assert cbor2.loads(cbor2.dumps(value)) == value


def test_file_dump_and_load():
    stream = io.BytesIO()
    cbor2.dump({"answer": 42}, stream)
    stream.seek(0)
    assert cbor2.load(stream) == {"answer": 42}


def test_encoder_decoder_stream():
    stream = io.BytesIO()
    encoder = cbor2.CBOREncoder(stream)
    encoder.encode([1, 2])
    encoder.encode({"x": 3})
    stream.seek(0)
    decoder = cbor2.CBORDecoder(stream)
    assert decoder.decode() == [1, 2]
    assert decoder.decode() == {"x": 3}


def test_default_encoder_hook():
    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    def default(encoder, value):
        encoder.encode(cbor2.CBORTag(4000, [value.x, value.y]))

    encoded = cbor2.dumps(Point(2, 3), default=default)
    assert cbor2.loads(encoded) == cbor2.CBORTag(4000, [2, 3])


def test_type_encoder_hook():
    class Box:
        def __init__(self, value):
            self.value = value

    def encode_box(encoder, value):
        encoder.encode(value.value)

    assert cbor2.loads(cbor2.dumps(Box(5), encoders={Box: encode_box})) == 5


def test_decoder_hooks():
    data = bytes.fromhex("d82aa1617801")
    tagged = cbor2.loads(
        data,
        object_hook=lambda value, immutable: ("map", value, immutable),
        tag_hook=lambda value, immutable: ("tag", value, immutable),
    )
    assert tagged[0] == "tag"
    assert tagged[1].value[0] == "map"
    assert tagged[1].value[2] is False


def test_utf8_error_policy():
    malformed_text = b"\x62\xff\x61"
    with pytest.raises(UnicodeDecodeError):
        cbor2.loads(malformed_text)
    assert cbor2.loads(malformed_text, str_errors="replace") == "\ufffda"


def test_custom_semantic_decoder():
    data = cbor2.dumps(cbor2.CBORTag(4001, [1, 2]))
    assert cbor2.loads(
        data, semantic_decoders={4001: lambda value, immutable: tuple(value)}
    ) == (1, 2)


def test_immutable_decode():
    value = cbor2.loads(cbor2.dumps({(1, 2): [3, 4]}), immutable=True)
    assert isinstance(value, cbor2.frozendict)
    assert value == {(1, 2): (3, 4)}
    assert hash(value)


def test_duplicate_key_policy():
    data = bytes.fromhex("a201020103")
    assert cbor2.loads(data) == {1: 3}
    with pytest.raises(cbor2.CBORDecodeError):
        cbor2.loads(data, allow_duplicate_keys=False)


def test_indefinite_policy():
    with pytest.raises(cbor2.CBORDecodeError):
        cbor2.loads(bytes.fromhex("9f01ff"), allow_indefinite=False)


@pytest.mark.parametrize("data", [b"", b"\x1a\x00", b"\x43\x01"])
def test_truncated_input(data):
    with pytest.raises(cbor2.CBORDecodeEOF):
        cbor2.loads(data)


@pytest.mark.parametrize("data", [b"\x1c", b"\xfc", b"\x9f\x01"])
def test_malformed_input(data):
    with pytest.raises(cbor2.CBORDecodeError):
        cbor2.loads(data)


def test_extra_data_rejected_by_loads():
    with pytest.raises(cbor2.CBORDecodeError):
        cbor2.loads(b"\x01\x02")


def test_max_depth():
    data = b"\x81" * 6 + b"\x00"
    with pytest.raises(cbor2.CBORDecodeError):
        cbor2.loads(data, max_depth=4)


def test_simple_value_validation():
    for value in (0, 19, 32, 255):
        assert cbor2.loads(cbor2.dumps(cbor2.CBORSimpleValue(value))).value == value
    assert (
        cbor2.loads(cbor2.dumps(cbor2.CBORSimpleValue(23))) is cbor2.undefined
    )
    for value in (-1, 24, 31, 256):
        with pytest.raises(ValueError):
            cbor2.CBORSimpleValue(value)


def test_float_special_values():
    for value in (math.inf, -math.inf):
        assert cbor2.loads(cbor2.dumps(value)) == value
    assert math.isnan(cbor2.loads(cbor2.dumps(math.nan)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.5, "f93e00"),
        (100000.0, "fa47c35000"),
        (1.1, "fb3ff199999999999a"),
    ],
)
def test_canonical_float_uses_preferred_width(value, expected):
    assert cbor2.dumps(value, canonical=True).hex() == expected


def test_bytes_like_inputs_and_outputs():
    value = bytearray(range(255))
    encoded = cbor2.dumps(memoryview(value))
    assert cbor2.loads(bytearray(encoded)) == bytes(value)


def test_naive_datetime_policy():
    value = datetime.datetime(2020, 1, 1)
    with pytest.raises(cbor2.CBOREncodeValueError):
        cbor2.dumps(value)
    decoded = cbor2.loads(
        cbor2.dumps(value, timezone=datetime.timezone.utc)
    )
    assert decoded.tzinfo == datetime.timezone.utc


def test_unsupported_sharing_options_are_explicit():
    with pytest.raises(NotImplementedError):
        cbor2.dumps([], value_sharing=True)
    with pytest.raises(NotImplementedError):
        cbor2.dumps("", string_referencing=True)


@pytest.mark.parametrize("length", [0, 1, 7, 8, 15, 31, 32, 33, 65])
def test_simd_payload_copy_tail(length):
    value = bytes((index * 17) & 0xFF for index in range(length))
    assert cbor2.loads(cbor2.dumps(value)) == value


@pytest.mark.parametrize(
    "length", [(4 << 20) - 1, 4 << 20, (4 << 20) + 17]
)
def test_parallel_payload_copy_threshold(length):
    value = bytes(range(251)) * (length // 251) + bytes(range(length % 251))
    assert cbor2.loads(cbor2.dumps(value)) == value


@pytest.mark.parametrize("length", [63, 64, 65])
def test_unsigned_array_fast_path_threshold(length):
    boundaries = [0, 23, 24, 255, 256, 65535, 65536, 2**32, 2**64 - 1]
    value = [boundaries[index % len(boundaries)] for index in range(length)]
    assert cbor2.loads(cbor2.dumps(value)) == value


def test_unsigned_array_fast_path_falls_back_for_other_scalar_types():
    value = list(range(62)) + [-1, True]
    decoded = cbor2.loads(cbor2.dumps(value))
    assert decoded == value
    assert decoded[-1] is True


@pytest.mark.parametrize("length", [255, 256, 257])
def test_flat_map_array_fast_path_threshold_and_fallback(monkeypatch, length):
    values = [
        {
            "id": index,
            "active": index % 3 == 0,
            "name": f"record-{index}",
            b"raw": bytes((index & 0xFF,)),
            7: None,
        }
        for index in range(length)
    ]
    encoded = cbor2.dumps(values)

    monkeypatch.setattr(cbor2, "_encode_flat_map_array", lambda obj, options: None)
    assert encoded == cbor2.dumps(values)

    decoded = cbor2.loads(encoded)
    monkeypatch.setattr(cbor2, "_decode_flat_map_array", lambda *args, **kwargs: None)
    assert decoded == cbor2.loads(encoded) == values


def test_flat_map_array_fast_path_rejects_nested_values_safely(monkeypatch):
    values = [{"id": index, "nested": [index, index + 1]} for index in range(256)]
    encoded = cbor2.dumps(values)
    monkeypatch.setattr(cbor2, "_encode_flat_map_array", lambda obj, options: None)
    assert encoded == cbor2.dumps(values)
    assert cbor2.loads(encoded) == values


def test_numpy_payload_stays_zero_copy_at_ffi_boundary(monkeypatch):
    from cbor2 import _lib

    value = np.arange(257, dtype=np.uint8)
    seen = []
    original = _lib.writable_buffer_addr

    def record_address(view):
        address = original(view)
        seen.append(address)
        return address

    monkeypatch.setattr(_lib, "writable_buffer_addr", record_address)
    assert cbor2.loads(cbor2.dumps(memoryview(value))) == value.tobytes()
    assert seen == [value.ctypes.data]


def test_noncontiguous_numpy_buffers_are_copied_safely():
    value = np.arange(32, dtype=np.uint8)[::2]
    assert cbor2.loads(cbor2.dumps(memoryview(value))) == value.tobytes()
    encoded = cbor2.dumps(value.tobytes())
    assert cbor2.loads(memoryview(np.frombuffer(encoded, dtype=np.uint8))) == value.tobytes()


def test_native_boundary_rejects_invalid_tape_shapes_and_dtypes():
    from cbor2 import _lib

    with pytest.raises(ValueError, match="four-word"):
        _lib.encode(__import__("array").array("Q", [1]), 1)
    with pytest.raises(TypeError, match="unsigned 64-bit"):
        _lib.tape_addr(__import__("array").array("I", [1]))
