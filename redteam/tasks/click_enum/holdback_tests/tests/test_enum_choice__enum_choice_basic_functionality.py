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


def test_enum_choice_basic_functionality():
    """Test that EnumChoice accepts valid enum names and returns enum instances."""
    param_type = click.EnumChoice(Color)

    # Test converting valid enum name to enum instance
    result = param_type.convert("RED", None, None)
    assert result is Color.RED
    assert isinstance(result, Color)

    # Test case-sensitive matching by default
    result = param_type.convert("GREEN", None, None)
    assert result is Color.GREEN
    assert result.value == "green"
