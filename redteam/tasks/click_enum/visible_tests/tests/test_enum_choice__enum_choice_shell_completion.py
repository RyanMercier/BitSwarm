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


def test_enum_choice_shell_completion():
    """Test that EnumChoice provides shell completion for enum names."""
    param_type = click.EnumChoice(Color)
    mock_ctx = mock.MagicMock()
    mock_param = mock.MagicMock()

    # Test completion with empty incomplete
    completions = param_type.shell_complete(mock_ctx, mock_param, "")
    completion_values = [c.value for c in completions]
    assert "RED" in completion_values
    assert "GREEN" in completion_values
    assert "BLUE" in completion_values

    # Test completion with partial match
    completions = param_type.shell_complete(mock_ctx, mock_param, "R")
    completion_values = [c.value for c in completions]
    assert "RED" in completion_values
    assert "GREEN" not in completion_values
