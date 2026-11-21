"""Integration tests for cross-subtask interaction."""

import pytest

from wordle.game import Game

from wordle.words import load_dictionary, is_valid_word, get_random_word

from wordle.scorer import score_guess

from wordle.renderer import render_board, render_guess

from wordle.cli import play_game

from wordle.types import Feedback, GameStatus


@pytest.mark.xfail(raises=NotImplementedError, strict=False)
def test_dictionary_and_validation_integration():
    """Test word dictionary loading and validation integration."""
    # Load dictionary
    dictionary = load_dictionary()
    assert len(dictionary) > 0

    # Test with a word from dictionary
    random_word = get_random_word()
    assert is_valid_word(random_word) is True
    assert len(random_word) == 5

    # Test scoring with valid words
    feedback = score_guess(random_word, random_word)
    assert len(feedback) == 5
    assert all(f == Feedback.HIT for f in feedback)
