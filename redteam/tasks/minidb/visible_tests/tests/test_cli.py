"""Unit tests for minidb.cli. The Engine is mocked throughout."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from minidb.cli import format_result, format_value, main, run_repl
from minidb.types import QueryResult, SQLSyntaxError, UnknownTableError


def _run(script: str, engine):
    stdin, stdout, stderr = io.StringIO(script), io.StringIO(), io.StringIO()
    code = run_repl(stdin, stdout, stderr, engine)
    return code, stdout.getvalue(), stderr.getvalue()


def test_format_value():
    assert format_value(None) == "NULL"
    assert format_value(True) == "TRUE"
    assert format_value(False) == "FALSE"
    assert format_value(0) == "0"
    assert format_value(-5) == "-5"
    assert format_value("abc") == "abc"


def test_format_result_select_table():
    result = QueryResult(
        columns=["id", "name", "ok"],
        rows=[(1, "alice", True), (22, None, False)],
        row_count=2,
    )
    lines = [l.rstrip() for l in format_result(result).rstrip("\n").split("\n")]
    assert lines == [
        "id | name  | ok",
        "-- | ----- | -----",
        "1  | alice | TRUE",
        "22 | NULL  | FALSE",
        "(2 rows)",
    ]


def test_format_result_single_row_singular():
    result = QueryResult(columns=["a"], rows=[(1,)], row_count=1)
    lines = format_result(result).rstrip("\n").split("\n")
    assert lines[-1] == "(1 row)"
    assert lines[0].rstrip() == "a"
    assert lines[1].rstrip() == "-"


def test_format_result_zero_rows():
    result = QueryResult(columns=["name"], rows=[], row_count=0)
    lines = [l.rstrip() for l in format_result(result).rstrip("\n").split("\n")]
    assert lines == ["name", "----", "(0 rows)"]


def test_format_result_non_select():
    assert format_result(QueryResult(row_count=3)).strip() == "OK (3 rows affected)"
    assert format_result(QueryResult(row_count=1)).strip() == "OK (1 rows affected)"


def test_repl_executes_each_statement():
    engine = MagicMock()
    engine.execute.side_effect = [QueryResult(row_count=0), QueryResult(row_count=1)]
    code, out, err = _run("CREATE TABLE t (a INTEGER);\nINSERT INTO t VALUES (1);\n", engine)
    assert code == 0
    assert err == ""
    assert engine.execute.call_count == 2
    assert "CREATE TABLE t (a INTEGER)" in engine.execute.call_args_list[0].args[0]
    assert "OK (0 rows affected)" in out
    assert "OK (1 rows affected)" in out
    assert out.index("OK (0") < out.index("OK (1")


def test_repl_accumulates_multiline():
    engine = MagicMock()
    engine.execute.return_value = QueryResult(columns=["a"], rows=[(1,)], row_count=1)
    code, out, err = _run("SELECT a\nFROM t\n;\n", engine)
    assert code == 0
    engine.execute.assert_called_once()
    sql = engine.execute.call_args.args[0]
    assert "SELECT a" in sql and "FROM t" in sql
    assert "(1 row)" in out


def test_repl_errors_go_to_stderr_and_continue():
    engine = MagicMock()
    engine.execute.side_effect = [
        UnknownTableError("nope"),
        SQLSyntaxError("bad syntax"),
        QueryResult(row_count=1),
    ]
    code, out, err = _run("a;\nb;\nc;\n", engine)
    assert code == 0
    assert "ERROR: unknown table: nope" in err
    assert "ERROR: bad syntax" in err
    assert err.count("ERROR: ") == 2
    assert "ERROR" not in out
    assert "OK (1 rows affected)" in out


def test_repl_exit_stops():
    engine = MagicMock()
    engine.execute.return_value = QueryResult(row_count=0)
    code, out, err = _run("a;\n  .exit \nb;\n", engine)
    assert code == 0
    assert engine.execute.call_count == 1


def test_repl_runs_leftover_buffer_at_eof():
    engine = MagicMock()
    engine.execute.return_value = QueryResult(row_count=1)
    code, out, err = _run("INSERT INTO t VALUES (9)", engine)
    assert code == 0
    engine.execute.assert_called_once()
    assert "INSERT INTO t VALUES (9)" in engine.execute.call_args.args[0]
    assert "OK (1 rows affected)" in out


def test_repl_blank_input_executes_nothing():
    engine = MagicMock()
    code, out, err = _run("\n   \n\n", engine)
    assert code == 0
    engine.execute.assert_not_called()
    assert "OK" not in out and "rows" not in out
    assert err == ""


def test_repl_default_engine():
    fake = MagicMock()
    fake.execute.return_value = QueryResult(row_count=0)
    with patch("minidb.cli.Engine", MagicMock(return_value=fake)) as cls:
        code, out, err = _run("CREATE TABLE t (a INTEGER);\n", None)
    assert code == 0
    cls.assert_called_once_with()
    assert "OK (0 rows affected)" in out


def test_main_returns_zero_on_eof():
    fake = MagicMock()
    with patch("minidb.cli.Engine", MagicMock(return_value=fake)), \
            patch("sys.stdin", io.StringIO("")), \
            patch("sys.stdout", io.StringIO()), \
            patch("sys.stderr", io.StringIO()):
        assert main([]) == 0
    fake.execute.assert_not_called()
