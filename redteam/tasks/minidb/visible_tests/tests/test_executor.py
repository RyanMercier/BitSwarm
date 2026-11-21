"""Unit tests for minidb.executor.

The storage backend and WHERE matcher are mocked so these tests depend
only on minidb.types and minidb.executor.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from minidb.executor import (
    execute,
    execute_create,
    execute_delete,
    execute_drop,
    execute_insert,
    execute_select,
)
from minidb.types import (
    ColumnDef,
    ColumnRef,
    ColumnType,
    Comparison,
    CreateTableStmt,
    DeleteStmt,
    DropTableStmt,
    InsertStmt,
    Literal,
    QueryResult,
    Schema,
    SelectStmt,
    TypeMismatchError,
    UnknownColumnError,
)

SCHEMA = Schema(
    (
        ColumnDef("a", ColumnType.INTEGER),
        ColumnDef("b", ColumnType.TEXT),
        ColumnDef("c", ColumnType.BOOLEAN),
    )
)
ROWS = [(1, "x", True), (2, "y", False), (3, None, True)]
WHERE = Comparison("=", ColumnRef("c"), Literal(True))


def make_store(rows=ROWS) -> MagicMock:
    store = MagicMock()
    store.get_schema.return_value = SCHEMA
    store.scan.side_effect = lambda name: iter(list(rows))
    return store


def never_called(*args):
    raise AssertionError("matcher must not be called")


def test_create_calls_store_and_returns_zero():
    store = MagicMock()
    stmt = CreateTableStmt("t", SCHEMA.columns)
    result = execute(stmt, store, never_called)
    assert result == QueryResult(columns=[], rows=[], row_count=0)
    store.create_table.assert_called_once_with("t", SCHEMA)


def test_execute_create_direct():
    store = MagicMock()
    result = execute_create(CreateTableStmt("t", SCHEMA.columns), store)
    assert result.row_count == 0
    assert store.create_table.call_args.args[1] == SCHEMA


def test_drop_calls_store():
    store = MagicMock()
    result = execute_drop(DropTableStmt("t"), store)
    assert result == QueryResult([], [], 0)
    store.drop_table.assert_called_once_with("t")


def test_insert_without_columns_passes_values_through():
    store = make_store()
    result = execute_insert(InsertStmt("t", None, (7, "z", False)), store)
    assert result == QueryResult([], [], 1)
    store.insert.assert_called_once_with("t", (7, "z", False))


def test_insert_with_column_list_maps_to_schema_order():
    store = make_store()
    result = execute(InsertStmt("t", ("c", "a"), (True, 5)), store, never_called)
    assert result.row_count == 1
    store.insert.assert_called_once_with("t", (5, None, True))


def test_insert_unknown_column_raises_without_insert():
    store = make_store()
    with pytest.raises(UnknownColumnError):
        execute_insert(InsertStmt("t", ("z",), (1,)), store)
    store.insert.assert_not_called()


def test_insert_count_mismatch_raises_without_insert():
    store = make_store()
    with pytest.raises(TypeMismatchError):
        execute_insert(InsertStmt("t", ("a", "b"), (1, "x", "y")), store)
    store.insert.assert_not_called()


def test_select_star_no_where():
    store = make_store()
    result = execute_select(SelectStmt("t", None, None), store, never_called)
    assert result.columns == ["a", "b", "c"]
    assert result.rows == ROWS
    assert result.row_count == 3
    store.get_schema.assert_called_with("t")
    store.scan.assert_called_with("t")


def test_select_projection_and_matcher():
    store = make_store()
    matcher = MagicMock(side_effect=lambda expr, row, schema: row[2] is True)
    result = execute(SelectStmt("t", ("b", "a"), WHERE), store, matcher)
    assert result.columns == ["b", "a"]
    assert result.rows == [("x", 1), (None, 3)]
    assert result.row_count == 2
    assert matcher.call_count == 3
    expr, row, schema = matcher.call_args_list[0].args
    assert expr is WHERE
    assert row == ROWS[0]
    assert schema == SCHEMA


def test_select_rows_are_tuples():
    store = make_store()
    result = execute_select(SelectStmt("t", ("a",), None), store, never_called)
    assert result.rows == [(1,), (2,), (3,)]
    assert all(type(r) is tuple for r in result.rows)


def test_select_unknown_column_raises_before_scan():
    store = make_store(rows=[])
    with pytest.raises(UnknownColumnError) as excinfo:
        execute_select(SelectStmt("t", ("a", "zz"), None), store, never_called)
    assert excinfo.value.column == "zz"
    store.scan.assert_not_called()


def test_delete_with_where_uses_matcher_in_predicate():
    store = make_store()
    captured = {}

    def fake_delete_where(name, predicate):
        captured["name"] = name
        return sum(1 for row in ROWS if predicate(row))

    store.delete_where.side_effect = fake_delete_where
    matcher = MagicMock(side_effect=lambda expr, row, schema: row[0] >= 2)
    result = execute_delete(DeleteStmt("t", WHERE), store, matcher)
    assert result == QueryResult([], [], 2)
    assert captured["name"] == "t"
    assert matcher.call_args_list[0].args == (WHERE, ROWS[0], SCHEMA)


def test_delete_without_where_removes_all():
    store = make_store()
    store.delete_where.side_effect = lambda name, pred: sum(1 for r in ROWS if pred(r))
    result = execute(DeleteStmt("t", None), store, never_called)
    assert result.row_count == 3
    assert result.columns == [] and result.rows == []
