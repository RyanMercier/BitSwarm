"""Unit tests for minidb.expr_eval."""

from __future__ import annotations

import pytest

from minidb.expr_eval import (
    compare,
    evaluate,
    logical_and,
    logical_not,
    logical_or,
    where_matches,
)
from minidb.types import (
    And,
    ColumnDef,
    ColumnRef,
    ColumnType,
    Comparison,
    IsNull,
    Literal,
    Not,
    Or,
    Schema,
    TypeMismatchError,
    UnknownColumnError,
)

SCHEMA = Schema(
    (
        ColumnDef("id", ColumnType.INTEGER),
        ColumnDef("name", ColumnType.TEXT),
        ColumnDef("active", ColumnType.BOOLEAN),
    )
)
ROW = (2, "bob", False)
NULL_ROW = (None, None, None)


def test_literal_and_column_ref():
    assert evaluate(Literal(5), ROW, SCHEMA) == 5
    assert evaluate(ColumnRef("name"), ROW, SCHEMA) == "bob"
    assert evaluate(ColumnRef("active"), ROW, SCHEMA) is False


def test_unknown_column_raises():
    with pytest.raises(UnknownColumnError) as excinfo:
        evaluate(ColumnRef("age"), ROW, SCHEMA)
    assert excinfo.value.column == "age"


@pytest.mark.parametrize(
    "op,left,right,expected",
    [
        ("=", 1, 1, True),
        ("=", 1, 2, False),
        ("<>", 1, 2, True),
        ("<", 1, 2, True),
        ("<=", 2, 2, True),
        (">", 3, 2, True),
        (">=", 1, 2, False),
        ("<", "alice", "bob", True),
        ("=", "a", "a", True),
        ("=", True, True, True),
        ("<>", True, False, True),
    ],
)
def test_compare_same_types(op, left, right, expected):
    assert compare(op, left, right) is expected


def test_compare_null_yields_null():
    assert compare("=", None, 1) is None
    assert compare("<", "a", None) is None
    assert compare("=", None, None) is None


def test_compare_mixed_types_raises():
    with pytest.raises(TypeMismatchError):
        compare("=", 1, "1")
    with pytest.raises(TypeMismatchError):
        compare("=", True, 1)
    with pytest.raises(TypeMismatchError):
        compare("<", 0, False)


def test_logical_not():
    assert logical_not(True) is False
    assert logical_not(False) is True
    assert logical_not(None) is None
    with pytest.raises(TypeMismatchError):
        logical_not(1)


def test_logical_and_kleene():
    assert logical_and(True, True) is True
    assert logical_and(True, False) is False
    assert logical_and(None, False) is False
    assert logical_and(False, None) is False
    assert logical_and(None, True) is None
    assert logical_and(None, None) is None
    with pytest.raises(TypeMismatchError):
        logical_and("x", True)


def test_logical_or_kleene():
    assert logical_or(False, False) is False
    assert logical_or(None, True) is True
    assert logical_or(True, None) is True
    assert logical_or(None, False) is None
    assert logical_or(None, None) is None
    with pytest.raises(TypeMismatchError):
        logical_or(False, 0)


def test_evaluate_comparison_on_row():
    expr = Comparison(">=", ColumnRef("id"), Literal(2))
    assert evaluate(expr, ROW, SCHEMA) is True
    assert evaluate(expr, NULL_ROW, SCHEMA) is None


def test_is_null_never_null():
    assert evaluate(IsNull(ColumnRef("name")), NULL_ROW, SCHEMA) is True
    assert evaluate(IsNull(ColumnRef("name"), negated=True), NULL_ROW, SCHEMA) is False
    assert evaluate(IsNull(ColumnRef("name")), ROW, SCHEMA) is False
    assert evaluate(IsNull(ColumnRef("name"), negated=True), ROW, SCHEMA) is True


def test_evaluate_nested_logic():
    expr = Or(Not(ColumnRef("active")), Comparison("=", ColumnRef("id"), Literal(1)))
    assert evaluate(expr, ROW, SCHEMA) is True
    assert evaluate(expr, (3, "c", True), SCHEMA) is False
    assert evaluate(expr, (3, "c", None), SCHEMA) is None


def test_not_on_non_boolean_column_raises():
    with pytest.raises(TypeMismatchError):
        evaluate(Not(ColumnRef("name")), ROW, SCHEMA)
    with pytest.raises(TypeMismatchError):
        evaluate(And(ColumnRef("id"), ColumnRef("active")), ROW, SCHEMA)


def test_where_matches_true_false_null():
    assert where_matches(Comparison("=", ColumnRef("id"), Literal(2)), ROW, SCHEMA) is True
    assert where_matches(Comparison("=", ColumnRef("id"), Literal(3)), ROW, SCHEMA) is False
    assert where_matches(Literal(None), ROW, SCHEMA) is False
    assert where_matches(Comparison("=", ColumnRef("id"), Literal(2)), NULL_ROW, SCHEMA) is False


def test_where_matches_bare_boolean_column():
    assert where_matches(ColumnRef("active"), (1, "a", True), SCHEMA) is True
    assert where_matches(ColumnRef("active"), ROW, SCHEMA) is False


def test_where_matches_non_boolean_raises():
    with pytest.raises(TypeMismatchError):
        where_matches(ColumnRef("id"), ROW, SCHEMA)
    with pytest.raises(TypeMismatchError):
        where_matches(Literal("x"), ROW, SCHEMA)
