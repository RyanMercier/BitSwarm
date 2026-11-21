"""Integration tests for cross-subtask interaction."""

import pytest

from wordle.game import Game

from wordle.words import load_dictionary, is_valid_word, get_random_word

from wordle.scorer import score_guess

from wordle.renderer import render_board, render_guess

from wordle.cli import play_game

from wordle.types import Feedback, GameStatus


@pytest.mark.xfail(raises=NotImplementedError, strict=False)
def test_error_handling_integration():
    """Test error handling across modules."""
    from wordle.types import InvalidGuessError, GameOverError

    game = Game("HELLO")

    # Test invalid guess error
    try:
        game.make_guess("HI")  # Too short
        assert False, "Should raise InvalidGuessError"
    except InvalidGuessError:
        pass  # Expected

    # Win the game
    game.make_guess("HELLO")

    # Test game over error
    try:
        game.make_guess("WORLD")  # Game already won
        assert False, "Should raise GameOverError"
    except GameOverError:
        pass  # Expected
