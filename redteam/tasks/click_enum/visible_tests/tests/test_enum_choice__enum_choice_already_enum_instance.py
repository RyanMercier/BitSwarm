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


def test_enum_choice_already_enum_instance():
    """Test that EnumChoice handles when value is already an enum instance."""
    param_type = click.EnumChoice(Color)

    # Should pass through enum instances unchanged
    result = param_type.convert(Color.RED, None, None)
    assert result is Color.RED
    assert isinstance(result, Color)
