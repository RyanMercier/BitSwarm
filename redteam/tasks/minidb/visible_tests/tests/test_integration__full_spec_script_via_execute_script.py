"""Integration tests for MiniDB.

These tests exercise the full pipeline (tokenizer -> parser -> executor ->
expr_eval -> table_store) through ``minidb.engine.Engine`` and the REPL in
``minidb.cli``, plus the explicit wiring between modules.

Signatures relied on (the contract for every subtask):

* ``minidb.tokenizer.tokenize(sql: str) -> list[Token]``
* ``minidb.parser.parse(tokens) -> Statement``
* ``minidb.parser.parse_script(tokens) -> list[Statement]``
* ``minidb.parser.parse_expression(tokens) -> Expr``
* ``minidb.table_store.TableStore()`` (no-arg constructor)
* ``minidb.expr_eval.where_matches(expr, row, schema) -> bool``
* ``minidb.executor.execute(stmt, store, matches) -> QueryResult``
* ``minidb.engine.Engine(store: Optional[StorageBackend] = None)`` with
  public ``store`` attribute, ``execute(sql)`` and ``execute_script(sql)``
* ``minidb.cli.format_value(value) -> str``
* ``minidb.cli.format_result(result) -> str``
* ``minidb.cli.run_repl(stdin, stdout, stderr, engine=None) -> int``
"""

from __future__ import annotations

import io

from unittest.mock import MagicMock, patch

import pytest

import minidb

import minidb.cli as cli

import minidb.engine as engine_module

import minidb.executor as executor

import minidb.expr_eval as expr_eval

import minidb.parser as parser

import minidb.table_store as table_store

import minidb.tokenizer as tokenizer

from minidb.cli import format_result, format_value, run_repl

from minidb.engine import Engine

from minidb.executor import execute

from minidb.expr_eval import where_matches

from minidb.parser import parse, parse_expression, parse_script

from minidb.table_store import TableStore

from minidb.tokenizer import tokenize

from minidb.types import (
    And,
    ColumnDef,
    ColumnRef,
    ColumnType,
    Comparison,
    CreateTableStmt,
    IsNull,
    Literal,
    MiniDBError,
    QueryResult,
    Schema,
    SelectStmt,
    SQLSyntaxError,
    TableExistsError,
    TokenType,
    TypeMismatchError,
    UnknownColumnError,
    UnknownTableError,
)

xfail_stub = pytest.mark.xfail(raises=NotImplementedError, strict=False)

SETUP_SQL = [
    "CREATE TABLE users (id INTEGER, name TEXT, email TEXT, active BOOLEAN);",
    "INSERT INTO users (id, name, email, active) VALUES (1, 'alice', 'a@x.com', TRUE);",
    "INSERT INTO users (id, name, email, active) VALUES (2, 'bob',   'b@x.com', FALSE);",
    "INSERT INTO users (id, name, email, active) VALUES (3, 'carol', NULL,       TRUE);",
]

ALICE = (1, "alice", "a@x.com", True)

BOB = (2, "bob", "b@x.com", False)

CAROL = (3, "carol", None, True)

def make_engine() -> Engine:
    engine = Engine()
    for sql in SETUP_SQL:
        engine.execute(sql)
    return engine

def names(result: QueryResult) -> list[str]:
    return [row[0] for row in result.rows]

def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.rstrip("\n").split("\n")]

def _run(script: str, engine=None):
    stdin = io.StringIO(script)
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_repl(stdin, stdout, stderr, engine)
    return code, stdout.getvalue(), stderr.getvalue()


@xfail_stub
def test_full_spec_script_via_execute_script():
    engine = Engine()
    script = "\n".join(SETUP_SQL) + """
        SELECT * FROM users;
        SELECT name, email FROM users WHERE active = TRUE;
        SELECT name FROM users WHERE id >= 2 AND email IS NOT NULL;
        SELECT name FROM users WHERE NOT active OR id = 1;
        DELETE FROM users WHERE active = FALSE;
        DROP TABLE users;
    """
    results = engine.execute_script(script)
    assert len(results) == 10
    assert [r.row_count for r in results] == [0, 1, 1, 1, 3, 2, 1, 2, 1, 0]
    assert results[4].rows == [ALICE, BOB, CAROL]
    assert results[5].rows == [("alice", "a@x.com"), ("carol", None)]
    assert results[6].rows == [("bob",)]
    assert results[7].rows == [("alice",), ("bob",)]
    assert engine.store.table_names() == []
