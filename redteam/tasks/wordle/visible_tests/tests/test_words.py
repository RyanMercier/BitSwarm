"""Tests for words module."""
import pytest
from wordle.words import load_dictionary, is_valid_word, get_random_word, normalize_word


def test_load_dictionary():
    """Test that load_dictionary returns a set of words."""
    dictionary = load_dictionary()
    assert isinstance(dictionary, set)
    assert len(dictionary) > 0


def test_is_valid_word():
    """Test word validation functionality."""
    # Test with a known valid word
    result = is_valid_word("HELLO")
    assert isinstance(result, bool)

    # Test with invalid length
    result = is_valid_word("HI")
    assert isinstance(result, bool)

    # Test with invalid characters
    result = is_valid_word("12345")
    assert isinstance(result, bool)


def test_get_random_word():
    """Test random word generation."""
    word = get_random_word()
    assert isinstance(word, str)
    assert len(word) == 5
    assert word.isupper()


def test_normalize_word():
    """Test word normalization."""
    result = normalize_word("hello")
    assert result == "HELLO"

    result = normalize_word(" WORLD ")
    assert result == "WORLD"

    result = normalize_word("Mixed")
    assert result == "MIXED"


def test_normalize_word_with_special_chars():
    """Test normalization with special characters."""
    result = normalize_word("he!!o")
    assert isinstance(result, str)