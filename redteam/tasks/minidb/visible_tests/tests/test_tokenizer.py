"""Unit tests for minidb.tokenizer."""

from __future__ import annotations

import pytest

from minidb.tokenizer import (
    is_identifier_char,
    is_identifier_start,
    read_string_literal,
    tokenize,
)
from minidb.types import SQLSyntaxError, Token, TokenType

K = TokenType.KEYWORD
ID = TokenType.IDENTIFIER
INT = TokenType.INTEGER
STR = TokenType.STRING
OP = TokenType.OPERATOR
EOF = Token(TokenType.EOF, "")


def toks(*pairs: tuple[TokenType, str]) -> list[Token]:
    return [Token(t, v) for t, v in pairs] + [EOF]


def test_empty_input_is_just_eof():
    result = tokenize("")
    assert result == [EOF]
    assert result[0].position == 0


def test_whitespace_only_is_just_eof_at_end_position():
    result = tokenize("  \n\t ")
    assert result == [EOF]
    assert result[0].position == 5


def test_keywords_normalized_identifiers_preserved():
    assert tokenize("select Name from Users") == toks(
        (K, "SELECT"), (ID, "Name"), (K, "FROM"), (ID, "Users")
    )


def test_mixed_case_keyword():
    assert tokenize("NuLl TrUe false") == toks((K, "NULL"), (K, "TRUE"), (K, "FALSE"))


def test_identifier_with_underscore_and_digits():
    assert tokenize("_col1 user_name2") == toks((ID, "_col1"), (ID, "user_name2"))


def test_keyword_prefix_identifier_is_identifier():
    assert tokenize("selection") == toks((ID, "selection"))


def test_integer_tokens():
    assert tokenize("42 007") == toks((INT, "42"), (INT, "007"))


def test_string_literal_with_escape():
    assert tokenize("'it''s'") == toks((STR, "it's"))


def test_empty_string_literal():
    assert tokenize("''") == toks((STR, ""))


def test_string_preserves_case_and_semicolon():
    assert tokenize("'Hello ; World'") == toks((STR, "Hello ; World"))


def test_operators_longest_match():
    assert tokenize("<> <= >= = < > ( ) , * ;") == toks(
        (OP, "<>"), (OP, "<="), (OP, ">="), (OP, "="), (OP, "<"), (OP, ">"),
        (OP, "("), (OP, ")"), (OP, ","), (OP, "*"), (OP, ";"),
    )


def test_operators_without_spaces():
    assert tokenize("a<=1") == toks((ID, "a"), (OP, "<="), (INT, "1"))
    assert tokenize("a<>b") == toks((ID, "a"), (OP, "<>"), (ID, "b"))


def test_full_statement_tokens():
    assert tokenize("INSERT INTO t (a, b) VALUES (1, 'x');") == toks(
        (K, "INSERT"), (K, "INTO"), (ID, "t"), (OP, "("), (ID, "a"), (OP, ","),
        (ID, "b"), (OP, ")"), (K, "VALUES"), (OP, "("), (INT, "1"), (OP, ","),
        (STR, "x"), (OP, ")"), (OP, ";"),
    )


def test_positions_recorded():
    result = tokenize("SELECT a FROM t")
    assert [t.position for t in result] == [0, 7, 9, 14, 15]


def test_string_position_is_opening_quote():
    result = tokenize("x = 'ab'")
    assert result[2].position == 4
    assert result[3].position == 8


def test_exactly_one_eof():
    result = tokenize("a ; b ;")
    assert [t.type for t in result].count(TokenType.EOF) == 1
    assert result[-1].type is TokenType.EOF


def test_unterminated_string_raises():
    with pytest.raises(SQLSyntaxError):
        tokenize("SELECT 'abc")


def test_unknown_character_raises():
    with pytest.raises(SQLSyntaxError):
        tokenize("a # b")
    with pytest.raises(SQLSyntaxError):
        tokenize("-1")


def test_is_identifier_start():
    assert is_identifier_start("a") is True
    assert is_identifier_start("Z") is True
    assert is_identifier_start("_") is True
    assert is_identifier_start("1") is False
    assert is_identifier_start("'") is False


def test_is_identifier_char():
    assert is_identifier_char("a") is True
    assert is_identifier_char("9") is True
    assert is_identifier_char("_") is True
    assert is_identifier_char(" ") is False
    assert is_identifier_char("(") is False


def test_read_string_literal():
    assert read_string_literal("x = 'ab''c' rest", 4) == ("ab'c", 11)
    assert read_string_literal("''", 0) == ("", 2)


def test_read_string_literal_unterminated():
    with pytest.raises(SQLSyntaxError):
        read_string_literal("'abc''", 0)
