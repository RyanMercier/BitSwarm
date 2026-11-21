"""Audit suite: minidb.table_store. Ground truth only; never shown to miners
and never used for scoring."""
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

SCHEMA = Schema((ColumnDef("id", ColumnType.INTEGER),
                 ColumnDef("name", ColumnType.TEXT),
                 ColumnDef("ok", ColumnType.BOOLEAN)))


def store():
    s = TableStore()
    s.create_table("t", SCHEMA)
    return s


def test_create_and_has_and_schema():
    s = store()
    assert s.has_table("t") and not s.has_table("x")
    assert s.get_schema("t") is SCHEMA
    assert "t" in s.table_names()


def test_duplicate_create_raises():
    s = store()
    with pytest.raises(TableExistsError):
        s.create_table("t", SCHEMA)


def test_unknown_table_operations_raise():
    s = TableStore()
    for call in (lambda: s.get_schema("x"), lambda: s.insert("x", (1, "a", True)),
                 lambda: list(s.scan("x")), lambda: s.drop_table("x"),
                 lambda: s.delete_where("x", lambda r: True)):
        with pytest.raises(UnknownTableError):
            call()


def test_insert_and_scan_preserve_order():
    s = store()
    for i in range(3):
        s.insert("t", (i, f"n{i}", i % 2 == 0))
    assert [r[0] for r in s.scan("t")] == [0, 1, 2]


def test_insert_type_and_arity_checks():
    s = store()
    with pytest.raises(TypeMismatchError):
        s.insert("t", (1, "a"))          # too few
    with pytest.raises(TypeMismatchError):
        s.insert("t", (1, "a", True, 9))  # too many
    with pytest.raises(TypeMismatchError):
        s.insert("t", ("x", "a", True))   # str for INTEGER
    with pytest.raises(TypeMismatchError):
        s.insert("t", (1, "a", 1))        # int for BOOLEAN
    with pytest.raises(TypeMismatchError):
        s.insert("t", (True, "a", True))  # bool for INTEGER


def test_null_allowed_for_any_column():
    s = store()
    s.insert("t", (None, None, None))
    assert list(s.scan("t")) == [(None, None, None)]


def test_scan_is_a_snapshot():
    s = store()
    s.insert("t", (1, "a", True))
    it = s.scan("t")
    s.insert("t", (2, "b", False))
    assert list(it) == [(1, "a", True)]


def test_delete_where_counts_and_preserves_order():
    s = store()
    for i in range(5):
        s.insert("t", (i, "n", True))
    removed = s.delete_where("t", lambda r: r[0] % 2 == 0)
    assert removed == 3
    assert [r[0] for r in s.scan("t")] == [1, 3]


def test_drop_removes_table():
    s = store()
    s.drop_table("t")
    assert not s.has_table("t")
