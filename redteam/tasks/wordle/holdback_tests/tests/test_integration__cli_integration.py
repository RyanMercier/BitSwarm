"""Integration tests for cross-subtask interaction."""

import pytest

from wordle.game import Game

from wordle.words import load_dictionary, is_valid_word, get_random_word

from wordle.scorer import score_guess

from wordle.renderer import render_board, render_guess

from wordle.cli import play_game

from wordle.types import Feedback, GameStatus


@pytest.mark.xfail(raises=NotImplementedError, strict=False)
def test_cli_integration():
    """Test CLI integration with game logic."""
    from unittest.mock import patch

    # Mock user input to simulate a game
    with patch('wordle.cli.get_user_input') as mock_input:
        with patch('builtins.print'):  # Suppress output
            mock_input.side_effect = ["HELLO", "WORLD", "TESTS"]

            # This should run without errors when implemented
            play_game("TESTS")
