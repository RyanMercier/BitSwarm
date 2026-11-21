"""Audit suite: minidb.cli. Ground truth only; never shown to miners and
never used for scoring."""
import io

from minidb.cli import format_result, format_value, main, run_repl
from minidb.types import ColumnType  # noqa: F401 (ensures package import)
from minidb.types import QueryResult


def test_format_value_cases():
    assert format_value(None) == "NULL"
    assert format_value(True) == "TRUE"
    assert format_value(False) == "FALSE"
    assert format_value(42) == "42"
    assert format_value("hi") == "hi"


def test_format_result_select_table():
    res = QueryResult(columns=["id", "name"], rows=[(1, "alice")], row_count=1)
    out = format_result(res)
    lines = out.split("\n")
    assert lines[0].startswith("id")
    assert set(lines[1]) <= {"-", " ", "|"}
    assert "alice" in lines[2]
    assert lines[-1] == "(1 row)"


def test_format_result_plural_rows():
    res = QueryResult(columns=["x"], rows=[(1,), (2,)], row_count=2)
    assert format_result(res).endswith("(2 rows)")


def test_format_result_non_select():
    res = QueryResult(columns=[], rows=[], row_count=3)
    assert format_result(res) == "OK (3 rows affected)"


def test_format_result_columns_align():
    res = QueryResult(columns=["id", "name"],
                      rows=[(1, "alice"), (22, "b")], row_count=2)
    lines = format_result(res).split("\n")
    # every content line is the same width (left-justified columns)
    widths = {len(ln) for ln in lines[:-1]}
    assert len(widths) == 1


def test_repl_executes_and_prints():
    stdin = io.StringIO(
        "CREATE TABLE t (id INTEGER, name TEXT);\n"
        "INSERT INTO t VALUES (1, 'alice');\n"
        "SELECT * FROM t;\n")
    out, err = io.StringIO(), io.StringIO()
    assert run_repl(stdin, out, err) == 0
    text = out.getvalue()
    assert "alice" in text
    assert "(1 row)" in text
    assert err.getvalue() == ""


def test_repl_multiline_statement():
    stdin = io.StringIO("CREATE TABLE t (\n id INTEGER\n);\n")
    out, err = io.StringIO(), io.StringIO()
    run_repl(stdin, out, err)
    assert "OK" in out.getvalue()
    assert err.getvalue() == ""


def test_repl_errors_go_to_stderr_and_continue():
    stdin = io.StringIO("SELECT * FROM nope;\nSELECT 1;\n")
    out, err = io.StringIO(), io.StringIO()
    run_repl(stdin, out, err)
    assert "ERROR:" in err.getvalue()


def test_repl_exit_command_stops():
    stdin = io.StringIO(".exit\nCREATE TABLE t (x INTEGER);\n")
    out, err = io.StringIO(), io.StringIO()
    from minidb.engine import Engine
    eng = Engine()
    run_repl(stdin, out, err, eng)
    assert not eng.store.has_table("t")


def test_repl_blank_lines_produce_no_output():
    stdin = io.StringIO("\n   \n")
    out, err = io.StringIO(), io.StringIO()
    run_repl(stdin, out, err)
    assert out.getvalue() == "" and err.getvalue() == ""


def test_repl_leftover_without_semicolon_runs_at_eof():
    stdin = io.StringIO("CREATE TABLE t (x INTEGER)")
    out, err = io.StringIO(), io.StringIO()
    from minidb.engine import Engine
    eng = Engine()
    run_repl(stdin, out, err, eng)
    assert eng.store.has_table("t")


def test_main_returns_zero(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    assert main() == 0
