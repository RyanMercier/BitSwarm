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


def test_enum_choice_with_different_enum_types():
    """Test EnumChoice works with different enum types."""
    # Test with IntEnum
    int_param = click.EnumChoice(Status)
    result = int_param.convert("ACTIVE", None, None)
    assert result is Status.ACTIVE
    assert result.value == 1

    # Test with mixed value enum
    mixed_param = click.EnumChoice(Priority)
    result = mixed_param.convert("HIGH", None, None)
    assert result is Priority.HIGH
    assert result.value == "high"

    result = mixed_param.convert("CRITICAL", None, None)
    assert result is Priority.CRITICAL
    assert result.value == 99
