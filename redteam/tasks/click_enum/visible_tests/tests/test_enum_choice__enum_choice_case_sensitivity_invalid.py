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


def test_enum_choice_case_sensitivity_invalid():
    """Test that case-sensitive enum rejects wrong case."""
    param_type = click.EnumChoice(Color, case_sensitive=True)
    mock_param = mock.MagicMock()

    # Should reject lowercase when case sensitive
    with pytest.raises(BadParameter):
        param_type.convert("red", mock_param, None)
