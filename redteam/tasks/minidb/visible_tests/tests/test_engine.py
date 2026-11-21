"""Unit tests for minidb.engine.

The pipeline functions are patched on ``minidb.engine`` so these tests
exercise only the Engine wiring.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import minidb.engine as engine_module
from minidb.engine import Engine
from minidb.types import QueryResult, SQLSyntaxError, UnknownTableError


def test_explicit_store_is_used():
    store = MagicMock()
    engine = Engine(store)
    assert engine.store is store
    assert Engine(store=store).store is store


def test_default_store_comes_from_module_table_store():
    fake = MagicMock()
    with patch("minidb.engine.TableStore", MagicMock(return_value=fake)) as ts:
        engine = Engine()
    ts.assert_called_once_with()
    assert engine.store is fake


def test_execute_runs_pipeline():
    store = MagicMock()
    tokens = [MagicMock()]
    stmt = MagicMock()
    expected = QueryResult(columns=["a"], rows=[(1,)], row_count=1)
    with patch("minidb.engine.tokenize", MagicMock(return_value=tokens)) as tok, \
            patch("minidb.engine.parse", MagicMock(return_value=stmt)) as prs, \
            patch("minidb.engine.execute", MagicMock(return_value=expected)) as exe:
        result = Engine(store).execute("SELECT a FROM t")
    assert result is expected
    tok.assert_called_once_with("SELECT a FROM t")
    prs.assert_called_once_with(tokens)
    args = exe.call_args.args
    assert args[0] is stmt
    assert args[1] is store
    assert args[2] is engine_module.where_matches


def test_execute_syntax_error_from_parse_propagates_and_skips_execute():
    with patch("minidb.engine.tokenize", MagicMock(return_value=[])), \
            patch("minidb.engine.parse", MagicMock(side_effect=SQLSyntaxError("bad"))), \
            patch("minidb.engine.execute", MagicMock()) as exe:
        engine = Engine(MagicMock())
        with pytest.raises(SQLSyntaxError):
            engine.execute("")
    exe.assert_not_called()


def test_execute_propagates_execution_errors():
    with patch("minidb.engine.tokenize", MagicMock(return_value=[])), \
            patch("minidb.engine.parse", MagicMock(return_value=MagicMock())), \
            patch("minidb.engine.execute", MagicMock(side_effect=UnknownTableError("x"))):
        engine = Engine(MagicMock())
        with pytest.raises(UnknownTableError):
            engine.execute("SELECT * FROM x")


def test_execute_script_runs_each_statement_in_order():
    store = MagicMock()
    tokens = [MagicMock()]
    stmts = [MagicMock(), MagicMock(), MagicMock()]
    results = [QueryResult(row_count=0), QueryResult(row_count=1), QueryResult(row_count=1)]
    with patch("minidb.engine.tokenize", MagicMock(return_value=tokens)) as tok, \
            patch("minidb.engine.parse_script", MagicMock(return_value=stmts)) as ps, \
            patch("minidb.engine.execute", MagicMock(side_effect=results)) as exe:
        out = Engine(store).execute_script("a; b; c;")
    assert out == results
    tok.assert_called_once_with("a; b; c;")
    ps.assert_called_once_with(tokens)
    assert [c.args[0] for c in exe.call_args_list] == stmts
    assert all(c.args[1] is store for c in exe.call_args_list)
    assert all(c.args[2] is engine_module.where_matches for c in exe.call_args_list)


def test_execute_script_empty_returns_empty_list():
    with patch("minidb.engine.tokenize", MagicMock(return_value=[])), \
            patch("minidb.engine.parse_script", MagicMock(return_value=[])), \
            patch("minidb.engine.execute", MagicMock()) as exe:
        out = Engine(MagicMock()).execute_script("")
    assert out == []
    exe.assert_not_called()


def test_execute_script_syntax_error_executes_nothing():
    with patch("minidb.engine.tokenize", MagicMock(return_value=[])), \
            patch("minidb.engine.parse_script", MagicMock(side_effect=SQLSyntaxError("x"))), \
            patch("minidb.engine.execute", MagicMock()) as exe:
        engine = Engine(MagicMock())
        with pytest.raises(SQLSyntaxError):
            engine.execute_script("CREATE TABLE t (a INTEGER); SELEC")
    exe.assert_not_called()
