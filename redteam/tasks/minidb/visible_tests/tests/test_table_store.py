"""Unit tests for minidb.table_store."""

from __future__ import annotations

import pytest

from minidb.table_store import TableStore
from minidb.types import (
    ColumnDef,
    ColumnType,
    Schema,
    TableExistsError,
    TypeMismatchError,
    UnknownTableError,
)

SCHEMA = Schema(
    (
        ColumnDef("id", ColumnType.INTEGER),
        ColumnDef("name", ColumnType.TEXT),
        ColumnDef("ok", ColumnType.BOOLEAN),
    )
)


def make_store() -> TableStore:
    store = TableStore()
    store.create_table("t", SCHEMA)
    return store


def test_create_and_get_schema():
    store = make_store()
    assert store.has_table("t") is True
    assert store.get_schema("t") == SCHEMA
    assert store.table_names() == ["t"]


def test_new_store_is_empty():
    store = TableStore()
    assert store.table_names() == []
    assert store.has_table("t") is False


def test_table_names_case_sensitive():
    store = make_store()
    assert store.has_table("T") is False
    store.create_table("T", SCHEMA)
    assert sorted(store.table_names()) == ["T", "t"]


def test_create_existing_raises():
    store = make_store()
    with pytest.raises(TableExistsError) as excinfo:
        store.create_table("t", SCHEMA)
    assert excinfo.value.table == "t"


def test_insert_and_scan_in_order():
    store = make_store()
    store.insert("t", (1, "a", True))
    store.insert("t", (2, None, None))
    store.insert("t", (3, "c", False))
    assert list(store.scan("t")) == [(1, "a", True), (2, None, None), (3, "c", False)]


def test_scan_empty_table():
    store = make_store()
    assert list(store.scan("t")) == []


def test_insert_wrong_arity_raises():
    store = make_store()
    with pytest.raises(TypeMismatchError):
        store.insert("t", (1, "a"))
    with pytest.raises(TypeMismatchError):
        store.insert("t", (1, "a", True, 4))
    assert list(store.scan("t")) == []


@pytest.mark.parametrize(
    "row",
    [("1", "a", True), (True, "a", True), (1, 2, True), (1, "a", 1), (1, "a", "TRUE")],
)
def test_insert_wrong_type_raises(row):
    store = make_store()
    with pytest.raises(TypeMismatchError):
        store.insert("t", row)
    assert list(store.scan("t")) == []


def test_scan_is_snapshot():
    store = make_store()
    store.insert("t", (1, "a", True))
    it = store.scan("t")
    store.insert("t", (2, "b", True))
    assert list(it) == [(1, "a", True)]


def test_delete_where_returns_count_and_preserves_order():
    store = make_store()
    for i in range(1, 6):
        store.insert("t", (i, str(i), i % 2 == 0))
    removed = store.delete_where("t", lambda row: row[2] is True)
    assert removed == 2
    assert [r[0] for r in store.scan("t")] == [1, 3, 5]


def test_delete_where_none_match():
    store = make_store()
    store.insert("t", (1, "a", True))
    assert store.delete_where("t", lambda row: False) == 0
    assert list(store.scan("t")) == [(1, "a", True)]


def test_drop_table_removes_rows():
    store = make_store()
    store.insert("t", (1, "a", True))
    store.drop_table("t")
    assert store.has_table("t") is False
    assert store.table_names() == []
    store.create_table("t", SCHEMA)
    assert list(store.scan("t")) == []


def test_unknown_table_errors():
    store = TableStore()
    with pytest.raises(UnknownTableError) as excinfo:
        store.get_schema("ghost")
    assert excinfo.value.table == "ghost"
    with pytest.raises(UnknownTableError):
        store.drop_table("ghost")
    with pytest.raises(UnknownTableError):
        store.insert("ghost", (1,))
    with pytest.raises(UnknownTableError):
        list(store.scan("ghost"))
    with pytest.raises(UnknownTableError):
        store.delete_where("ghost", lambda row: True)
