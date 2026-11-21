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


def test_enum_choice_invalid_input():
    """Test that EnumChoice raises BadParameter for invalid inputs."""
    param_type = click.EnumChoice(Color)

    # Mock parameter for error context
    mock_param = mock.MagicMock()
    mock_param.name = "color"

    # Test invalid enum name
    with pytest.raises(BadParameter) as exc_info:
        param_type.convert("YELLOW", mock_param, None)

    error_msg = str(exc_info.value)
    # Should contain the invalid value and list valid choices
    assert "YELLOW" in error_msg
    assert "RED" in error_msg or "GREEN" in error_msg or "BLUE" in error_msg
