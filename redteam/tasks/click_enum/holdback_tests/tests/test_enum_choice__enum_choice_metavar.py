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


def test_enum_choice_metavar():
    """Test that EnumChoice provides appropriate metavar for help."""
    param_type = click.EnumChoice(Color)
    mock_ctx = mock.MagicMock()
    mock_param = mock.MagicMock()
    mock_param.required = False
    mock_param.param_type_name = "option"
    mock_param.show_choices = True

    metavar = param_type.get_metavar(mock_param, mock_ctx)
    assert metavar is not None
    # Should contain the enum choice names
    assert "RED" in metavar or "GREEN" in metavar or "BLUE" in metavar
