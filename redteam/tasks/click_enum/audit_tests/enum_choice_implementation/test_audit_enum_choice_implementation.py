"""Audit suite: click.types.EnumChoice. Ground truth only; never shown to
miners and never used for scoring.

Behavior comes from the subtask's behavior_spec and the target stub's
docstrings: the user supplies an enum member NAME, the callback gets the
member, matching is case-sensitive by default, errors list the valid
names, help and completion follow the Choice patterns. Imports come from
click.types so these tests do not depend on the export subtask.
"""
import enum

import click
import pytest
from click.testing import CliRunner
from click.types import EnumChoice


class Color(str, enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Shape(enum.Enum):
    CIRCLE = enum.auto()
    SQUARE = enum.auto()


def test_converts_member_name_to_member():
    assert EnumChoice(Color).convert("GREEN", None, None) is Color.GREEN
    assert EnumChoice(Shape).convert("SQUARE", None, None) is Shape.SQUARE


def test_matching_is_case_sensitive_by_default():
    with pytest.raises(click.BadParameter):
        EnumChoice(Color).convert("green", None, None)


def test_case_insensitive_mode_matches_any_case():
    t = EnumChoice(Color, case_sensitive=False)
    assert t.convert("gReEn", None, None) is Color.GREEN
    assert t.convert("BLUE", None, None) is Color.BLUE


def test_names_not_values_are_accepted():
    t = EnumChoice(Priority)
    assert t.convert("HIGH", None, None) is Priority.HIGH
    with pytest.raises(click.BadParameter):
        t.convert("2", None, None)


def test_enum_instance_passes_through():
    assert EnumChoice(Color).convert(Color.RED, None, None) is Color.RED


def test_invalid_choice_message_lists_valid_names():
    with pytest.raises(click.BadParameter) as exc:
        EnumChoice(Color).convert("PURPLE", None, None)
    msg = str(exc.value)
    assert "PURPLE" in msg
    for name in ("RED", "GREEN", "BLUE"):
        assert name in msg


def _cmd(**opt_kwargs):
    @click.command()
    @click.option("--color", type=EnumChoice(Color), **opt_kwargs)
    def cmd(color):
        if isinstance(color, tuple):
            click.echo(",".join(c.name for c in color))
        else:
            assert color is None or isinstance(color, Color)
            click.echo(f"got={color.name if color else None}")
    return cmd


def test_option_end_to_end():
    res = CliRunner().invoke(_cmd(), ["--color", "BLUE"])
    assert res.exit_code == 0, res.output
    assert "got=BLUE" in res.output


def test_option_rejects_bad_value_with_usage_error():
    res = CliRunner().invoke(_cmd(), ["--color", "PURPLE"])
    assert res.exit_code == 2
    assert "RED" in res.output and "PURPLE" in res.output


def test_help_lists_choices():
    res = CliRunner().invoke(_cmd(), ["--help"])
    assert res.exit_code == 0
    for name in ("RED", "GREEN", "BLUE"):
        assert name in res.output


def test_default_enum_member_is_used():
    res = CliRunner().invoke(_cmd(default=Color.GREEN), [])
    assert res.exit_code == 0, res.output
    assert "got=GREEN" in res.output


def test_multiple_values():
    res = CliRunner().invoke(_cmd(multiple=True), ["--color", "RED", "--color", "BLUE"])
    assert res.exit_code == 0, res.output
    assert "RED,BLUE" in res.output


def test_argument_usage():
    @click.command()
    @click.argument("shape", type=EnumChoice(Shape))
    def cmd(shape):
        click.echo(f"shape={shape.name}")

    res = CliRunner().invoke(cmd, ["CIRCLE"])
    assert res.exit_code == 0, res.output
    assert "shape=CIRCLE" in res.output


def test_shell_completion_prefixes():
    cmd = _cmd()
    ctx = click.Context(cmd)
    param = cmd.params[0]
    names = [c.value for c in EnumChoice(Color).shell_complete(ctx, param, "G")]
    assert names == ["GREEN"]
    all_names = [c.value for c in EnumChoice(Color).shell_complete(ctx, param, "")]
    assert sorted(all_names) == ["BLUE", "GREEN", "RED"]
    ci = EnumChoice(Color, case_sensitive=False).shell_complete(ctx, param, "b")
    assert [c.value for c in ci] == ["BLUE"]


def test_info_dict_describes_the_type():
    info = EnumChoice(Color, case_sensitive=False).to_info_dict()
    assert info["enum_type"] is Color
    assert info["case_sensitive"] is False
    assert info["name"] == "enum choice"
