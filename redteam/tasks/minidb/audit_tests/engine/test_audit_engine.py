"""Audit suite: minidb.engine. Ground truth only; never shown to miners
and never used for scoring. Drives the full stack through the public API."""
import pytest

from minidb.engine import Engine
from minidb.table_store import TableStore
from minidb.types import (
    SQLSyntaxError,
    TypeMismatchError,
    UnknownTableError,
)


def test_execute_single_statement_end_to_end():
    e = Engine()
    assert e.execute("CREATE TABLE t (id INTEGER, name TEXT)").row_count == 0
    assert e.execute("INSERT INTO t VALUES (1, 'a')").row_count == 1
    res = e.execute("SELECT * FROM t")
    assert res.columns == ["id", "name"] and res.rows == [(1, "a")]


def test_default_store_is_a_table_store():
    e = Engine()
    assert isinstance(e.store, TableStore)


def test_execute_uses_injected_store():
    store = TableStore()
    e = Engine(store)
    e.execute("CREATE TABLE t (x INTEGER)")
    assert store.has_table("t")


def test_blank_input_raises_syntax_error():
    e = Engine()
    with pytest.raises(SQLSyntaxError):
        e.execute("   ")


def test_multiple_statements_to_execute_raises():
    e = Engine()
    with pytest.raises(SQLSyntaxError):
        e.execute("CREATE TABLE a (x INTEGER); CREATE TABLE b (y INTEGER)")


def test_domain_errors_propagate():
    e = Engine()
    with pytest.raises(UnknownTableError):
        e.execute("SELECT * FROM nope")
    e.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(TypeMismatchError):
        e.execute("INSERT INTO t VALUES ('a')")


def test_execute_script_runs_in_order():
    e = Engine()
    results = e.execute_script(
        "CREATE TABLE t (id INTEGER);"
        "INSERT INTO t VALUES (1);"
        "INSERT INTO t VALUES (2);"
        "SELECT * FROM t;")
    assert [r.row_count for r in results] == [0, 1, 1, 2]
    assert results[-1].rows == [(1,), (2,)]


def test_execute_script_empty_returns_empty():
    assert Engine().execute_script("") == []
    assert Engine().execute_script(";;") == []


def test_script_syntax_error_runs_nothing():
    e = Engine()
    with pytest.raises(SQLSyntaxError):
        e.execute_script("CREATE TABLE t (x INTEGER); BOGUS")
    # whole script parsed before executing, so t must not exist
    assert not e.store.has_table("t")


def test_full_spec_scenario():
    e = Engine()
    e.execute_script(
        "CREATE TABLE users (id INTEGER, name TEXT, email TEXT, active BOOLEAN);"
        "INSERT INTO users (id, name, email, active) VALUES (1, 'alice', 'a@x.com', TRUE);"
        "INSERT INTO users (id, name, email, active) VALUES (2, 'bob', 'b@x.com', FALSE);"
        "INSERT INTO users (id, name, email, active) VALUES (3, 'carol', NULL, TRUE);")
    active = e.execute("SELECT name FROM users WHERE active = TRUE")
    assert active.rows == [("alice",), ("carol",)]
    with_email = e.execute("SELECT name FROM users WHERE id >= 2 AND email IS NOT NULL")
    assert with_email.rows == [("bob",)]
    e.execute("DELETE FROM users WHERE active = FALSE")
    assert e.execute("SELECT id FROM users").rows == [(1,), (3,)]
