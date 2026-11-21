"""Audit suite: minidb.parser. Ground truth only; never shown to miners
and never used for scoring."""
import pytest

from minidb.parser import parse, parse_expression, parse_script
from minidb.tokenizer import tokenize
from minidb.types import (
    And,
    ColumnRef,
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
)


def p(sql):
    return parse(tokenize(sql))


def e(sql):
    return parse_expression(tokenize(sql))


def test_create_table():
    stmt = p("CREATE TABLE users (id INTEGER, name TEXT, ok BOOLEAN)")
    assert isinstance(stmt, CreateTableStmt) and stmt.table == "users"
    assert [c.name for c in stmt.columns] == ["id", "name", "ok"]
    assert [c.type.value for c in stmt.columns] == ["INTEGER", "TEXT", "BOOLEAN"]


def test_drop_table():
    assert p("DROP TABLE t") == DropTableStmt("t")


def test_insert_with_and_without_columns():
    a = p("INSERT INTO t (id, name) VALUES (1, 'x')")
    assert isinstance(a, InsertStmt) and a.columns == ("id", "name")
    assert a.values == (1, "x")
    b = p("INSERT INTO t VALUES (1, 'x', TRUE)")
    assert b.columns is None and b.values == (1, "x", True)


def test_insert_literals_cover_all_types():
    stmt = p("INSERT INTO t VALUES (1, 'a', TRUE, FALSE, NULL)")
    assert stmt.values == (1, "a", True, False, None)


def test_select_star_and_projection_and_where():
    assert p("SELECT * FROM t").columns is None
    s = p("SELECT a, b FROM t WHERE a = 1")
    assert s.columns == ("a", "b")
    assert s.where == Comparison("=", ColumnRef("a"), Literal(1))


def test_delete_with_and_without_where():
    assert p("DELETE FROM t").where is None
    assert isinstance(p("DELETE FROM t WHERE x = 1"), DeleteStmt)


def test_optional_trailing_semicolon():
    assert isinstance(p("SELECT * FROM t;"), SelectStmt)


def test_precedence_not_over_and_over_or():
    # NOT a = 1 OR b = 2 AND c = 3  ->  Or(Not(a=1), And(b=2, c=3))
    expr = e("NOT a = 1 OR b = 2 AND c = 3")
    assert isinstance(expr, Or)
    assert isinstance(expr.left, Not)
    assert isinstance(expr.right, And)


def test_parentheses_override_precedence():
    expr = e("(a = 1 OR b = 2) AND c = 3")
    assert isinstance(expr, And)
    assert isinstance(expr.left, Or)


def test_is_null_and_is_not_null():
    assert e("x IS NULL") == IsNull(ColumnRef("x"), False)
    assert e("x IS NOT NULL") == IsNull(ColumnRef("x"), True)


def test_comparisons_do_not_chain():
    with pytest.raises(SQLSyntaxError):
        e("a = b = c")


def test_left_associative_and_or():
    expr = e("a = 1 AND b = 2 AND c = 3")
    assert isinstance(expr, And) and isinstance(expr.left, And)


def test_errors_on_bad_input():
    for bad in ("", ";", "SELECT", "SELECT * FROM", "CREATE TABLE t (id)",
                "SELECT * FROM t WHERE", "GARBAGE", "SELECT * FROM t extra"):
        with pytest.raises(SQLSyntaxError):
            p(bad)


def test_parse_script_multiple_and_empty():
    stmts = parse_script(tokenize("CREATE TABLE t (a INTEGER); SELECT * FROM t;"))
    assert len(stmts) == 2
    assert parse_script(tokenize("")) == []
    assert parse_script(tokenize(";;")) == []


def test_parse_script_propagates_errors():
    with pytest.raises(SQLSyntaxError):
        parse_script(tokenize("SELECT * FROM t; BOGUS"))


def test_parse_expression_must_consume_all():
    with pytest.raises(SQLSyntaxError):
        parse_expression(tokenize("a = 1 b"))
