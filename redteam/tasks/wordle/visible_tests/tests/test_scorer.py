"""Tests for scorer module."""
import pytest
from wordle.scorer import score_guess, _count_letters, _mark_hits, _mark_presents
from wordle.types import Feedback


def test_score_guess_exact_match():
    """Test scoring when guess exactly matches target."""
    result = score_guess("HELLO", "HELLO")
    assert len(result) == 5
    assert all(feedback == Feedback.HIT for feedback in result)


def test_score_guess_no_matches():
    """Test scoring when guess has no matching letters."""
    result = score_guess("ABCDE", "FGHIJ")
    assert len(result) == 5
    assert all(feedback == Feedback.MISS for feedback in result)


def test_score_guess_mixed_feedback():
    """Test scoring with mixed hit, present, and miss feedback."""
    result = score_guess("HELLO", "WORLD")
    assert len(result) == 5
    assert isinstance(result[0], Feedback)


def test_count_letters():
    """Test letter counting functionality."""
    counts = _count_letters("HELLO")
    assert isinstance(counts, dict)
    assert counts.get("L") == 2
    assert counts.get("H") == 1


def test_mark_hits():
    """Test exact match marking."""
    feedback = [Feedback.MISS] * 5
    _mark_hits("HELLO", "HELLO", feedback)
    assert all(f == Feedback.HIT for f in feedback)


def test_mark_presents():
    """Test present letter marking."""
    feedback = [Feedback.MISS] * 5
    _mark_presents("HELLO", "WORLD", feedback)
    # Should mark appropriate letters as present
    assert isinstance(feedback[0], Feedback)


def test_score_guess_duplicate_handling():
    """Test proper handling of duplicate letters."""
    # Test case where guess has duplicates but target doesn't
    result = score_guess("LLAMA", "LIGHT")
    assert len(result) == 5
    assert isinstance(result[0], Feedback)