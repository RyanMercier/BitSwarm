"""Unit tests for minidb.parser.

Tokens are built by hand so these tests do not depend on the tokenizer.
"""

from __future__ import annotations

import pytest

from minidb.parser import parse, parse_expression, parse_script
from minidb.types import (
    And,
    ColumnDef,
    ColumnRef,
    ColumnType,
    Comparison,
    CreateTableStmt,
    DeleteStmt,
    DropTableStmt,
    InsertStmt,
    IsNull,
    Literal,
    Not,
    Or,
    SelectStmt,
    SQLSyntaxError,
    Token,
    TokenType,
)


def lex(text: str) -> list[Token]:
    """Tiny whitespace-split lexer for tests: each space-separated word is
    a token. Uppercase words in KEYWORD_SET -> KEYWORD; digits -> INTEGER;
    words wrapped in single quotes -> STRING; symbols -> OPERATOR; else
    IDENTIFIER."""
    keywords = {
        "CREATE", "TABLE", "DROP", "INSERT", "INTO", "VALUES", "SELECT",
        "FROM", "WHERE", "DELETE", "AND", "OR", "NOT", "IS", "NULL", "TRUE",
        "FALSE", "INTEGER", "TEXT", "BOOLEAN",
    }
    ops = {"<>", "<=", ">=", "=", "<", ">", "(", ")", ",", "*", ";"}
    out: list[Token] = []
    for word in text.split():
        if word in keywords:
            out.append(Token(TokenType.KEYWORD, word))
        elif word.isdigit():
            out.append(Token(TokenType.INTEGER, word))
        elif word.startswith("'") and word.endswith("'") and len(word) >= 2:
            out.append(Token(TokenType.STRING, word[1:-1]))
        elif word in ops:
            out.append(Token(TokenType.OPERATOR, word))
        else:
            out.append(Token(TokenType.IDENTIFIER, word))
    out.append(Token(TokenType.EOF, ""))
    return out


# --- statements -------------------------------------------------------------


def test_create_table():
    stmt = parse(lex("CREATE TABLE t ( a INTEGER , b TEXT , c BOOLEAN )"))
    assert stmt == CreateTableStmt(
        "t",
        (
            ColumnDef("a", ColumnType.INTEGER),
            ColumnDef("b", ColumnType.TEXT),
            ColumnDef("c", ColumnType.BOOLEAN),
        ),
    )


def test_drop_table_with_semicolon():
    assert parse(lex("DROP TABLE users ;")) == DropTableStmt("users")


def test_insert_without_columns_converts_literals():
    stmt = parse(lex("INSERT INTO t VALUES ( 1 , 'x' , TRUE , FALSE , NULL )"))
    assert stmt == InsertStmt("t", None, (1, "x", True, False, None))
    assert type(stmt.values[0]) is int
    assert type(stmt.values[2]) is bool


def test_insert_with_columns():
    stmt = parse(lex("INSERT INTO t ( c , a ) VALUES ( TRUE , 5 )"))
    assert stmt == InsertStmt("t", ("c", "a"), (True, 5))


def test_select_star():
    assert parse(lex("SELECT * FROM users")) == SelectStmt("users", None, None)


def test_select_columns_with_where():
    stmt = parse(lex("SELECT name , email FROM users WHERE active = TRUE"))
    assert stmt == SelectStmt(
        "users",
        ("name", "email"),
        Comparison("=", ColumnRef("active"), Literal(True)),
    )


def test_delete_with_and_without_where():
    assert parse(lex("DELETE FROM t")) == DeleteStmt("t", None)
    assert parse(lex("DELETE FROM t WHERE a <> 2")) == DeleteStmt(
        "t", Comparison("<>", ColumnRef("a"), Literal(2))
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        ";",
        "SELECT * FROM t ; SELECT * FROM t",
        "SELECT FROM t",
        "SELECT * FROM",
        "SELECT * FROM t WHERE",
        "SELECT * FROM t WHERE a = 1 = 1",
        "SELECT * FROM t WHERE ( a = 1",
        "INSERT INTO t VALUES ( a )",
        "INSERT INTO t VALUES ( )",
        "CREATE TABLE t ( a FLOAT )",
        "CREATE TABLE t ( )",
        "DROP t",
        "SELECT * FROM t ; ;",
        "users",
    ],
)
def test_parse_syntax_errors(text):
    with pytest.raises(SQLSyntaxError):
        parse(lex(text))


# --- scripts ----------------------------------------------------------------


def test_parse_script_multiple_and_skips_empty():
    stmts = parse_script(lex("DROP TABLE a ; ; DROP TABLE b ; DELETE FROM c ;"))
    assert stmts == [DropTableStmt("a"), DropTableStmt("b"), DeleteStmt("c")]


def test_parse_script_without_trailing_semicolon():
    assert parse_script(lex("DROP TABLE a ; DROP TABLE b")) == [
        DropTableStmt("a"),
        DropTableStmt("b"),
    ]


def test_parse_script_empty():
    assert parse_script(lex("")) == []
    assert parse_script(lex("; ;")) == []


def test_parse_script_error():
    with pytest.raises(SQLSyntaxError):
        parse_script(lex("DROP TABLE a ; SELECT a"))


# --- expressions ------------------------------------------------------------


def test_expression_precedence_not_and_or():
    expr = parse_expression(lex("NOT a OR b = 1 AND c"))
    assert expr == Or(
        Not(ColumnRef("a")),
        And(Comparison("=", ColumnRef("b"), Literal(1)), ColumnRef("c")),
    )


def test_not_binds_over_comparison():
    assert parse_expression(lex("NOT a = 1")) == Not(
        Comparison("=", ColumnRef("a"), Literal(1))
    )


def test_and_or_left_associative():
    assert parse_expression(lex("a AND b AND c")) == And(
        And(ColumnRef("a"), ColumnRef("b")), ColumnRef("c")
    )
    assert parse_expression(lex("a OR b OR c")) == Or(
        Or(ColumnRef("a"), ColumnRef("b")), ColumnRef("c")
    )


def test_parentheses_override():
    assert parse_expression(lex("( a OR b ) AND c")) == And(
        Or(ColumnRef("a"), ColumnRef("b")), ColumnRef("c")
    )


def test_is_null_and_is_not_null():
    assert parse_expression(lex("email IS NULL")) == IsNull(ColumnRef("email"), False)
    assert parse_expression(lex("email IS NOT NULL")) == IsNull(ColumnRef("email"), True)


def test_all_comparison_ops_and_literals():
    for op in ("=", "<>", "<", "<=", ">", ">="):
        assert parse_expression(lex(f"x {op} 'v'")) == Comparison(
            op, ColumnRef("x"), Literal("v")
        )
    assert parse_expression(lex("NULL")) == Literal(None)
    assert parse_expression(lex("FALSE")) == Literal(False)


def test_expression_trailing_tokens_error():
    with pytest.raises(SQLSyntaxError):
        parse_expression(lex("a b"))
    with pytest.raises(SQLSyntaxError):
        parse_expression(lex("a IS 1"))
