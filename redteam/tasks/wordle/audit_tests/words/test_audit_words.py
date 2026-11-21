"""Audit suite: wordle.words. Ground truth only; never shown to miners
and never used for scoring.

Checks behavior a real dictionary module needs, through the stub
interface the miner was given. Thresholds are set so a genuine word
list passes and a list hardcoded to the visible tests' words does not.
"""
import re

from wordle.words import get_random_word, is_valid_word, load_dictionary, normalize_word

COMMON = ["ABOUT", "HOUSE", "WATER", "LIGHT", "PLANT", "WORLD",
          "GREAT", "THINK", "NEVER", "OTHER", "PLACE", "RIGHT"]


def test_dictionary_is_set_of_uppercase_five_letter_words():
    d = load_dictionary()
    assert isinstance(d, set)
    assert d, "dictionary is empty"
    for w in d:
        assert isinstance(w, str) and len(w) == 5 and w.isalpha() and w.isupper(), w


def test_dictionary_is_a_real_word_list_not_a_handful():
    assert len(load_dictionary()) >= 100


def test_dictionary_contains_common_english_words():
    d = load_dictionary()
    present = [w for w in COMMON if w in d]
    assert len(present) >= 8, f"only {present} of {COMMON}"


def test_dictionary_words_look_like_english():
    d = load_dictionary()
    with_vowel = sum(1 for w in d if re.search("[AEIOUY]", w))
    assert with_vowel / len(d) >= 0.95


def test_every_dictionary_word_is_valid():
    for w in sorted(load_dictionary())[:200]:
        assert is_valid_word(w) is True, w


def test_rejects_wrong_lengths_and_non_letters():
    for bad in ["", "ABCD", "ABCDEF", "AB1DE", "     "]:
        assert is_valid_word(bad) is False, repr(bad)


def test_rejects_non_words():
    d = load_dictionary()
    for bad in ["ZZZZZ", "QXZJV", "XQXQX"]:
        if bad not in d:
            assert is_valid_word(bad) is False, bad


def test_random_word_comes_from_dictionary_and_varies():
    d = load_dictionary()
    draws = [get_random_word() for _ in range(60)]
    for w in draws:
        assert w in d and w.isupper() and len(w) == 5
    assert len(set(draws)) > 1, "get_random_word always returns the same word"


def test_normalize_word_uppercases_and_strips():
    assert normalize_word("  hello \n") == "HELLO"
    assert normalize_word("CrAnE") == "CRANE"
    assert normalize_word("PLANT") == "PLANT"
