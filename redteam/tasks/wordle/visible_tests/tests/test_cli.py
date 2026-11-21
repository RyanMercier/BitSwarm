"""Tests for CLI module."""
import pytest
from unittest.mock import MagicMock, patch
from wordle.cli import (
    main, play_game, get_user_input, display_welcome,
    display_game_state, display_result, handle_guess_error, ask_play_again
)
from wordle.types import InvalidGuessError, GameOverError


def test_main():
    """Test main function entry point."""
    with patch('wordle.cli.play_game') as mock_play:
        main()
        mock_play.assert_called_once()


def test_play_game():
    """Test playing a complete game."""
    # Mock the game to avoid dependency on actual Game implementation
    mock_game = MagicMock()
    mock_game.get_status.return_value.value = "won"

    with patch('wordle.cli.Game', return_value=mock_game):
        with patch('wordle.cli.get_user_input', return_value="HELLO"):
            with patch('wordle.cli.display_game_state'):
                with patch('wordle.cli.display_result'):
                    play_game("HELLO")
                    mock_game.make_guess.assert_called()


def test_get_user_input():
    """Test getting user input."""
    with patch('builtins.input', return_value="HELLO"):
        result = get_user_input()
        assert result == "HELLO"


def test_display_welcome():
    """Test welcome message display."""
    with patch('builtins.print') as mock_print:
        display_welcome()
        mock_print.assert_called()


def test_display_game_state():
    """Test game state display."""
    mock_game = MagicMock()
    mock_game.get_results.return_value = []
    mock_game.get_attempts_remaining.return_value = 6

    with patch('builtins.print') as mock_print:
        display_game_state(mock_game)
        mock_print.assert_called()


def test_display_result_win():
    """Test result display for winning game."""
    mock_game = MagicMock()
    mock_game.get_status.return_value.value = "won"
    mock_game.get_target_word.return_value = "HELLO"

    with patch('builtins.print') as mock_print:
        display_result(mock_game)
        mock_print.assert_called()


def test_handle_guess_error():
    """Test error handling for guess errors."""
    error = InvalidGuessError("Invalid word")

    with patch('builtins.print') as mock_print:
        handle_guess_error(error)
        mock_print.assert_called()


def test_ask_play_again():
    """Test asking user to play again."""
    with patch('builtins.input', return_value="y"):
        result = ask_play_again()
        assert result is True

    with patch('builtins.input', return_value="n"):
        result = ask_play_again()
        assert result is False