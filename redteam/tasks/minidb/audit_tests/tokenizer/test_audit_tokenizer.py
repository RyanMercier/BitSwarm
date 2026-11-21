"""Audit suite: minidb.tokenizer. Ground truth only; never shown to miners
and never used for scoring."""
import pytest

from minidb.tokenizer import (
    is_identifier_char,
    is_identifier_start,
    read_string_literal,
    tokenize,
)
from minidb.types import SQLSyntaxError, TokenType


def kinds(sql):
    return [(t.type, t.value) for t in tokenize(sql)]


def test_keywords_uppercased_identifiers_preserved():
    toks = tokenize("select Name from Users")
    assert toks[0].type is TokenType.KEYWORD and toks[0].value == "SELECT"
    assert toks[1].type is TokenType.IDENTIFIER and toks[1].value == "Name"
    assert toks[2].value == "FROM"
    assert toks[3].type is TokenType.IDENTIFIER and toks[3].value == "Users"


def test_stream_ends_with_eof():
    toks = tokenize("")
    assert toks[-1].type is TokenType.EOF and toks[-1].value == ""
    assert tokenize("   \n\t ")[-1].type is TokenType.EOF


def test_integer_and_string_and_operators():
    assert (TokenType.INTEGER, "42") in kinds("x = 42")
    s = tokenize("'hello'")
    assert s[0].type is TokenType.STRING and s[0].value == "hello"
    for op in ("<>", "<=", ">=", "=", "<", ">", "(", ")", ",", "*", ";"):
        assert (TokenType.OPERATOR, op) in kinds(f"a {op} b")


def test_two_char_operators_beat_one_char():
    assert [t.value for t in tokenize("a <> b")][1] == "<>"
    assert [t.value for t in tokenize("a <= b")][1] == "<="
    assert [t.value for t in tokenize("a >= b")][1] == ">="


def test_string_escapes_doubled_quote():
    assert read_string_literal("'it''s'", 0)[0] == "it's"
    tok = tokenize("'a''b'")[0]
    assert tok.value == "a'b"


def test_unterminated_string_raises():
    with pytest.raises(SQLSyntaxError):
        read_string_literal("'oops", 0)
    with pytest.raises(SQLSyntaxError):
        tokenize("'oops")


def test_keywords_case_insensitive():
    assert tokenize("Select")[0].value == "SELECT"
    assert tokenize("TRUE")[0].type is TokenType.KEYWORD
    assert tokenize("null")[0].value == "NULL"


def test_identifier_predicates():
    assert is_identifier_start("a") and is_identifier_start("_")
    assert not is_identifier_start("1") and not is_identifier_start(" ")
    assert is_identifier_char("a") and is_identifier_char("9") and is_identifier_char("_")
    assert not is_identifier_char("-")


def test_position_recorded():
    toks = tokenize("a = 1")
    assert toks[0].position == 0
    assert toks[2].position == 4


def test_unexpected_character_raises():
    with pytest.raises(SQLSyntaxError):
        tokenize("a @ b")


def test_no_whitespace_needed():
    assert [t.value for t in tokenize("a=1")][:3] == ["a", "=", "1"]
    assert [t.value for t in tokenize("(a,b)")] == ["(", "a", ",", "b", ")", ""]
