from __future__ import annotations

from app.core.short_code import (
    ALPHABET,
    CODE_PATTERN,
    DEFAULT_LENGTH,
    RESERVED_PREFIXES,
    generate,
    is_reserved,
    is_valid_shape,
)


def test_generate_has_default_length() -> None:
    assert len(generate()) == DEFAULT_LENGTH


def test_generate_uses_only_alphabet_characters() -> None:
    code = generate(length=64)
    assert all(ch in ALPHABET for ch in code)


def test_generate_is_shaped_valid() -> None:
    assert is_valid_shape(generate())


def test_generate_produces_varied_output() -> None:
    codes = {generate() for _ in range(50)}
    assert len(codes) > 1


def test_reserved_prefixes_are_all_valid_route_segments() -> None:
    for prefix in RESERVED_PREFIXES:
        assert prefix and "/" not in prefix


def test_is_reserved_true_for_known_prefix() -> None:
    assert is_reserved("api")
    assert is_reserved("healthz")


def test_is_reserved_false_for_ordinary_code() -> None:
    assert not is_reserved("abc123")


def test_shape_rejects_too_short() -> None:
    assert not is_valid_shape("abc")


def test_shape_accepts_minimum_length() -> None:
    assert is_valid_shape("abcd")


def test_shape_rejects_too_long() -> None:
    assert not is_valid_shape("a" * 33)


def test_shape_accepts_maximum_length() -> None:
    assert is_valid_shape("a" * 32)


def test_shape_rejects_disallowed_characters() -> None:
    for bad in ("abc/def", "abc def", "abc.def", "abc?def", "../../etc"):
        assert not is_valid_shape(bad)


def test_shape_accepts_underscore_and_hyphen() -> None:
    assert is_valid_shape("abc_-12")


def test_code_pattern_is_anchored_at_both_ends() -> None:
    assert CODE_PATTERN.match("abcd1234/etc") is None
    assert CODE_PATTERN.match("abcd\nmore") is None
