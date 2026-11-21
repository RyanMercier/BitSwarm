"""Audit suite: click.EnumChoice export. Ground truth only; never shown to
miners and never used for scoring.
"""
import enum

import click
import click.types
from click.testing import CliRunner


class Mode(enum.Enum):
    FAST = 1
    SAFE = 2


def test_enum_choice_is_exported_at_top_level():
    assert click.EnumChoice is click.types.EnumChoice


def test_from_import_works():
    from click import EnumChoice  # noqa: F401


def test_exported_type_is_a_param_type():
    assert issubclass(click.EnumChoice, click.ParamType)


def test_existing_exports_still_present():
    for name in ("Choice", "IntRange", "FloatRange", "Path", "option",
                 "argument", "command", "group", "echo"):
        assert hasattr(click, name), name


def test_top_level_name_works_end_to_end():
    @click.command()
    @click.option("--mode", type=click.EnumChoice(Mode))
    def cmd(mode):
        click.echo(f"mode={mode.name}")

    res = CliRunner().invoke(cmd, ["--mode", "SAFE"])
    assert res.exit_code == 0, res.output
    assert "mode=SAFE" in res.output
