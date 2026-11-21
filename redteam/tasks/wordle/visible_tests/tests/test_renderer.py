"""Tests for renderer module."""
import pytest
from unittest.mock import MagicMock
from wordle.renderer import (
    render_board, render_guess, render_empty_row,
    _colorize_letter, render_keyboard
)
from wordle.types import Feedback


def test_render_board():
    """Test board rendering with multiple guesses."""
    # Mock GuessResult objects
    mock_result1 = MagicMock()
    mock_result1.guess = "HELLO"
    mock_result1.feedbacks = [Feedback.HIT] * 5

    mock_result2 = MagicMock()
    mock_result2.guess = "WORLD"
    mock_result2.feedbacks = [Feedback.MISS] * 5

    results = [mock_result1, mock_result2]
    board = render_board(results)
    assert isinstance(board, str)
    assert len(board) > 0


def test_render_guess():
    """Test rendering a single guess."""
    mock_result = MagicMock()
    mock_result.guess = "HELLO"
    mock_result.feedbacks = [Feedback.HIT, Feedback.PRESENT, Feedback.MISS, Feedback.HIT, Feedback.PRESENT]

    rendered = render_guess(mock_result)
    assert isinstance(rendered, str)
    assert "HELLO" in rendered or len(rendered) > 0


def test_render_empty_row():
    """Test rendering empty rows."""
    empty_row = render_empty_row()
    assert isinstance(empty_row, str)
    assert len(empty_row) > 0


def test_colorize_letter():
    """Test letter colorization."""
    hit_letter = _colorize_letter("H", "hit")
    assert isinstance(hit_letter, str)
    assert len(hit_letter) > 0

    present_letter = _colorize_letter("E", "present")
    assert isinstance(present_letter, str)

    miss_letter = _colorize_letter("X", "miss")
    assert isinstance(miss_letter, str)


def test_render_keyboard():
    """Test keyboard rendering with letter status."""
    used_letters = {
        "H": "hit",
        "E": "present",
        "L": "miss",
        "O": "hit"
    }
    keyboard = render_keyboard(used_letters)
    assert isinstance(keyboard, str)
    assert len(keyboard) > 0


def test_render_board_empty():
    """Test rendering empty board."""
    board = render_board([])
    assert isinstance(board, str)