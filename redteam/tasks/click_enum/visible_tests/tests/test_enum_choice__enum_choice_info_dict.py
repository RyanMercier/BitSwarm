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


def test_enum_choice_info_dict():
    """Test that EnumChoice provides proper info dict for documentation."""
    param_type = click.EnumChoice(Color, case_sensitive=False)
    info = param_type.to_info_dict()

    assert info["param_type"] == "EnumChoice"
    assert info["name"] == "enum choice"
    assert info["enum_type"] is Color
    assert info["case_sensitive"] is False
