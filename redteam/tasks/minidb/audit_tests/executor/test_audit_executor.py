"""Audit suite: minidb.executor. Ground truth only; never shown to miners
and never used for scoring. Uses the real TableStore and where_matches so
the executor is exercised against genuine collaborators."""
import pytest

from minidb.executor import execute
from minidb.expr_eval import where_matches
from minidb.parser import parse
from minidb.table_store import TableStore
from minidb.tokenizer import tokenize
from minidb.types import (
    TypeMismatchError,
    UnknownColumnError,
    UnknownTableError,
)


def run(store, sql):
    return execute(parse(tokenize(sql)), store, where_matches)


def fresh():
    store = TableStore()
    run(store, "CREATE TABLE t (id INTEGER, name TEXT, ok BOOLEAN)")
    return store


def test_create_insert_select_roundtrip():
    s = fresh()
    assert run(s, "INSERT INTO t VALUES (1, 'alice', TRUE)").row_count == 1
    res = run(s, "SELECT * FROM t")
    assert res.columns == ["id", "name", "ok"]
    assert res.rows == [(1, "alice", True)]
    assert res.row_count == 1


def test_create_and_drop_report_zero():
    s = TableStore()
    assert run(s, "CREATE TABLE a (x INTEGER)").row_count == 0
    assert run(s, "DROP TABLE a").row_count == 0
    assert not s.has_table("a")


def test_projection_order_and_subset():
    s = fresh()
    run(s, "INSERT INTO t VALUES (1, 'alice', TRUE)")
    res = run(s, "SELECT name, id FROM t")
    assert res.columns == ["name", "id"]
    assert res.rows == [("alice", 1)]


def test_insert_with_columns_fills_missing_with_null():
    s = fresh()
    run(s, "INSERT INTO t (id) VALUES (7)")
    assert run(s, "SELECT * FROM t").rows == [(7, None, None)]


def test_insert_unknown_column_raises():
    s = fresh()
    with pytest.raises(UnknownColumnError):
        run(s, "INSERT INTO t (nope) VALUES (1)")


def test_insert_wrong_value_count_with_columns():
    s = fresh()
    with pytest.raises(TypeMismatchError):
        run(s, "INSERT INTO t (id, name) VALUES (1)")


def test_insert_type_mismatch_raises():
    s = fresh()
    with pytest.raises(TypeMismatchError):
        run(s, "INSERT INTO t VALUES ('x', 'a', TRUE)")


def test_where_filters_rows():
    s = fresh()
    for i, nm, ok in [(1, "a", True), (2, "b", False), (3, "c", True)]:
        run(s, f"INSERT INTO t VALUES ({i}, '{nm}', {'TRUE' if ok else 'FALSE'})")
    res = run(s, "SELECT id FROM t WHERE ok = TRUE")
    assert res.rows == [(1,), (3,)]


def test_where_three_valued_filters_null_out():
    s = fresh()
    run(s, "INSERT INTO t (id) VALUES (1)")   # name is NULL
    run(s, "INSERT INTO t VALUES (2, 'x', TRUE)")
    res = run(s, "SELECT id FROM t WHERE name = 'x'")
    assert res.rows == [(2,)]


def test_delete_returns_count_and_removes():
    s = fresh()
    for i in range(4):
        run(s, f"INSERT INTO t VALUES ({i}, 'n', TRUE)")
    assert run(s, "SELECT * FROM t WHERE id >= 2 AND id < 4").row_count == 2
    assert run(s, "DELETE FROM t WHERE id < 2").row_count == 2
    assert [r[0] for r in run(s, "SELECT id FROM t").rows] == [2, 3]


def test_delete_without_where_removes_all():
    s = fresh()
    run(s, "INSERT INTO t VALUES (1, 'a', TRUE)")
    assert run(s, "DELETE FROM t").row_count == 1
    assert run(s, "SELECT * FROM t").rows == []


def test_select_unknown_table_and_column():
    s = fresh()
    with pytest.raises(UnknownTableError):
        run(s, "SELECT * FROM nope")
    with pytest.raises(UnknownColumnError):
        run(s, "SELECT nope FROM t")
