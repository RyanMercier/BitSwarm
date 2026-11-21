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


def test_enum_choice_with_click_option():
    """Test EnumChoice integration with click options."""
    @click.command()
    @click.option("--color", type=click.EnumChoice(Color))
    def cmd(color):
        # Callback should receive the actual enum instance
        assert isinstance(color, Color)
        return color

    # Test with click runner
    from click.testing import CliRunner
    runner = CliRunner()

    # Test valid enum name
    result = runner.invoke(cmd, ["--color", "RED"])
    assert result.exit_code == 0

    # Test invalid enum name should fail
    result = runner.invoke(cmd, ["--color", "YELLOW"])
    assert result.exit_code != 0
    assert "YELLOW" in result.output
