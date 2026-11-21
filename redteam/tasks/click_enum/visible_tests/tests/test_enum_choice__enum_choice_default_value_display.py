"""Tests for EnumChoice parameter type."""

import enum

from unittest import mock

import pytest

import click

from click.exceptions import BadParameter

class Color(str, enum.Enum):
    """Test enum with string values."""
    RED = "red"
    GREEN = "green"
    BLUE = "blue"

class Status(enum.IntEnum):
    """Test enum with integer values."""
    ACTIVE = 1
    INACTIVE = 0
    PENDING = 2

class Priority(enum.Enum):
    """Test enum with mixed values."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = 99


def test_enum_choice_default_value_display():
    """Test that EnumChoice works correctly with show_default=True."""
    @click.command()
    @click.option("--color", type=click.EnumChoice(Color), default=Color.RED, show_default=True)
    def cmd(color):
        return color

    # Get help text to verify default is shown properly
    ctx = click.Context(cmd)
    help_record = cmd.get_params(ctx)[0].get_help_record(ctx)

    # Help text should contain the enum name, not the value
    assert help_record is not None
    help_text = help_record[1]
    assert "RED" in help_text  # Should show enum name as default
