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


def test_enum_choice_case_sensitivity():
    """Test case sensitivity behavior of EnumChoice."""
    # Case sensitive by default - should work with exact names
    case_sensitive = click.EnumChoice(Color, case_sensitive=True)
    result = case_sensitive.convert("BLUE", None, None)
    assert result is Color.BLUE

    # Case insensitive option - should work with different casing
    case_insensitive = click.EnumChoice(Color, case_sensitive=False)
    result = case_insensitive.convert("red", None, None)
    assert result is Color.RED

    result = case_insensitive.convert("Green", None, None)
    assert result is Color.GREEN
