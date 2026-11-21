"""Audit suite: minidb.expr_eval (three-valued logic). Ground truth only;
never shown to miners and never used for scoring."""
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
)

SCHEMA = Schema((ColumnDef("a", ColumnType.INTEGER),
                 ColumnDef("b", ColumnType.TEXT),
                 ColumnDef("ok", ColumnType.BOOLEAN)))
ROW = (5, "hi", True)


def test_literal_and_column_refs():
    assert evaluate(Literal(3), ROW, SCHEMA) == 3
    assert evaluate(ColumnRef("a"), ROW, SCHEMA) == 5
    assert evaluate(ColumnRef("b"), ROW, SCHEMA) == "hi"


def test_compare_basic():
    assert compare("=", 1, 1) is True
    assert compare("<>", 1, 2) is True
    assert compare("<", 1, 2) is True
    assert compare(">=", 2, 2) is True
    assert compare("=", "a", "a") is True
    assert compare("<", "a", "b") is True


def test_compare_with_null_is_null():
    for op in ("=", "<>", "<", "<=", ">", ">="):
        assert compare(op, None, 1) is None
        assert compare(op, 1, None) is None


def test_compare_type_mismatch_raises():
    with pytest.raises(TypeMismatchError):
        compare("=", 1, "a")
    with pytest.raises(TypeMismatchError):
        compare("=", True, 1)  # bool vs int are distinct types


def test_not_three_valued():
    assert logical_not(True) is False
    assert logical_not(False) is True
    assert logical_not(None) is None


def test_and_truth_table():
    assert logical_and(True, True) is True
    assert logical_and(True, False) is False
    assert logical_and(False, None) is False
    assert logical_and(True, None) is None
    assert logical_and(None, None) is None


def test_or_truth_table():
    assert logical_or(False, False) is False
    assert logical_or(True, False) is True
    assert logical_or(True, None) is True
    assert logical_or(False, None) is None
    assert logical_or(None, None) is None


def test_boolean_ops_reject_non_boolean():
    with pytest.raises(TypeMismatchError):
        logical_not(1)
    with pytest.raises(TypeMismatchError):
        logical_and(1, True)
    with pytest.raises(TypeMismatchError):
        logical_or("x", False)


def test_is_null_always_boolean():
    schema = Schema((ColumnDef("x", ColumnType.INTEGER),))
    assert evaluate(IsNull(ColumnRef("x"), False), (None,), schema) is True
    assert evaluate(IsNull(ColumnRef("x"), True), (None,), schema) is False
    assert evaluate(IsNull(ColumnRef("x"), False), (3,), schema) is False


def test_where_matches_filters_null_and_false_out():
    schema = Schema((ColumnDef("x", ColumnType.INTEGER),))
    assert where_matches(Comparison("=", ColumnRef("x"), Literal(1)), (1,), schema) is True
    assert where_matches(Comparison("=", ColumnRef("x"), Literal(1)), (2,), schema) is False
    # NULL comparison -> NULL -> row filtered out (not an error)
    assert where_matches(Comparison("=", ColumnRef("x"), Literal(1)), (None,), schema) is False


def test_where_matches_rejects_non_boolean_result():
    schema = Schema((ColumnDef("x", ColumnType.INTEGER),))
    with pytest.raises(TypeMismatchError):
        where_matches(ColumnRef("x"), (3,), schema)


def test_nested_expression_evaluation():
    expr = Or(Not(ColumnRef("ok")), Comparison(">", ColumnRef("a"), Literal(3)))
    assert evaluate(expr, ROW, SCHEMA) is True
