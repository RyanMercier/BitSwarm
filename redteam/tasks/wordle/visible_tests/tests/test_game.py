"""Tests for game module."""
import pytest
from unittest.mock import MagicMock
from wordle.game import Game
from wordle.types import GameStatus, InvalidGuessError, GameOverError


def test_game_init():
    """Test game initialization."""
    game = Game("HELLO")
    status = game.get_status()
    assert status == GameStatus.IN_PROGRESS

    attempts = game.get_attempts_remaining()
    assert attempts == 6


def test_make_guess_valid():
    """Test making a valid guess."""
    game = Game("HELLO")
    result = game.make_guess("WORLD")

    assert result.guess == "WORLD"
    assert len(result.feedbacks) == 5

    attempts = game.get_attempts_remaining()
    assert attempts == 5


def test_make_guess_winning():
    """Test winning the game with correct guess."""
    game = Game("HELLO")
    result = game.make_guess("HELLO")

    assert result.guess == "HELLO"
    status = game.get_status()
    assert status == GameStatus.WON


def test_get_results():
    """Test getting all guess results."""
    game = Game("HELLO")
    game.make_guess("WORLD")
    game.make_guess("GREAT")

    results = game.get_results()
    assert len(results) == 2
    assert results[0].guess == "WORLD"
    assert results[1].guess == "GREAT"


def test_get_target_word():
    """Test getting target word after game over."""
    game = Game("HELLO")

    # Target should be None during game
    target = game.get_target_word()
    assert target is None

    # Win the game
    game.make_guess("HELLO")
    target = game.get_target_word()
    assert target == "HELLO"


def test_reset_game():
    """Test resetting the game."""
    game = Game("HELLO")
    game.make_guess("WORLD")

    game.reset("TESTS")
    status = game.get_status()
    assert status == GameStatus.IN_PROGRESS

    attempts = game.get_attempts_remaining()
    assert attempts == 6

    results = game.get_results()
    assert len(results) == 0


def test_game_over_after_six_attempts():
    """Test game over after maximum attempts."""
    game = Game("HELLO")

    # Make 6 wrong guesses
    for _ in range(6):
        game.make_guess("WRONG")

    status = game.get_status()
    assert status == GameStatus.LOST