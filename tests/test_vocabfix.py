"""vocabfix: replacements and custom-word spellings on every transcript (the iPhone app's
VocabFixer tests, ported, plus replacements)."""

from __future__ import annotations

import pytest

from shoutabout.vocabfix import apply_fixes, distance, is_common_word, sound_key, split_camel_case

VOCAB = [
    "Sofia",
    "Better Shell",
    "ShoutAbout",
    "TestFlight",
    "Sample Project",
    "faster-whisper",
    "Claude",
    "GPU",
]


# ------------------------------------------------------------------ same spelling


def test_canonical_spelling():
    terms = ["Better Shell", "ShoutAbout", "GPU"]
    assert (
        apply_fixes("open better shell and ask shout about the gpu", terms)
        == "open Better Shell and ask ShoutAbout the GPU"
    )
    assert apply_fixes("bettershell, Better-Shell.", terms) == "Better Shell, Better Shell."


def test_word_boundaries():
    assert apply_fixes("gpus and gpu", ["GPU"]) == "gpus and GPU"
    assert apply_fixes("nothing here", []) == "nothing here"
    assert apply_fixes("", ["X"]) == ""


def test_longest_term_wins():
    assert apply_fixes("better shell pro rocks", ["Better Shell", "Better Shell Pro"]) == (
        "Better Shell Pro rocks"
    )


def test_camel_case_split():
    assert split_camel_case("ShoutAbout") == ["Shout", "About"]
    assert split_camel_case("GPU") == ["GPU"]
    assert split_camel_case("iPhone") == ["i", "Phone"]


def test_replacement_characters_are_literal():
    assert apply_fixes("pay us 5 dollars", ["$5 Dollars"]) == "pay us 5 dollars"
    assert apply_fixes("price is c sharp", ["C\\Sharp"]) == "price is c sharp"
    assert apply_fixes("use group one", [r"\1 group"]) == "use group one"


# ------------------------------------------------------------------- sound-alike


def test_sophia_becomes_sofia():
    assert apply_fixes("I talked to Sophia today.", ["Sofia"]) == "I talked to Sofia today."
    assert apply_fixes("Sophia's idea was great", ["Sofia"]) == "Sofia's idea was great"
    assert apply_fixes("Sophia called. Then Sophia left", VOCAB) == "Sofia called. Then Sofia left"


def test_better_shell():
    assert apply_fixes("open better shell now", VOCAB) == "open Better Shell now"
    assert apply_fixes("open bettor shell now", VOCAB) == "open Better Shell now"
    assert apply_fixes("Open Bettor Shell now", VOCAB) == "Open Better Shell now"


def test_shoutabout():
    assert apply_fixes("tell shout about", VOCAB) == "tell ShoutAbout"
    assert apply_fixes("tell Shout Aboot about it", VOCAB) == "tell ShoutAbout about it"
    assert apply_fixes("ask Shoutabaut", VOCAB) == "ask ShoutAbout"
    # Common words in lowercase are never rewritten by sound.
    assert apply_fixes("we'll shoot about ten photos", VOCAB) == "we'll shoot about ten photos"
    assert apply_fixes("tell shout aboot", VOCAB) == "tell shout aboot"
    # A different number of words is never a match.
    assert apply_fixes("shout a boat", VOCAB) == "shout a boat"


@pytest.mark.parametrize(
    "sentence",
    [
        "I sat on the sofa with my phone",
        "a simple project for the team",
        "the cloud is gray and we sell shells",
        "We'd better sell the shell, then test the flight.",
        "Cloud storage is cheap",
        "the gpus and the graphics",
    ],
)
def test_ordinary_sentences_stay_alone(sentence):
    assert apply_fixes(sentence, VOCAB) == sentence


def test_common_words_need_a_capital_in_mid_sentence():
    marks = "Mark the date, then call Mark."
    assert apply_fixes(marks, ["Marc"]) == "Mark the date, then call Marc."
    assert apply_fixes("I asked Cloud for help", ["Claude"]) == "I asked Claude for help"


def test_longest_sound_alike_wins():
    assert apply_fixes("bettor shell pro rocks", ["Better Shell", "Better Shell Pro"]) == (
        "Better Shell Pro rocks"
    )
    assert apply_fixes("Sophia Copola directed it", ["Sofia", "Sofia Coppola"]) == (
        "Sofia Coppola directed it"
    )


def test_exact_terms_are_never_rewritten():
    assert apply_fixes("Sophia and Sofia", ["Sophia", "Sofia"]) == "Sophia and Sofia"


def test_short_words_and_sentence_breaks():
    assert apply_fixes("Zoey said hi", ["Zoe"]) == "Zoey said hi"
    assert apply_fixes("Sophia.\nLoren", ["Sofia Loren"]) == "Sophia.\nLoren"


def test_names_and_lowercase_terms():
    assert apply_fixes("Hi Phillip", ["Philip"]) == "Hi Philip"
    assert apply_fixes("Hi Stephen", ["Steven"]) == "Hi Steven"
    assert apply_fixes("Hi Jon", ["John"]) == "Hi John"
    assert apply_fixes("use Postgress here", ["postgres"]) == "use postgres here"
    assert apply_fixes("Faster wisper is fast", VOCAB) == "Faster-whisper is fast"


def test_sound_keys():
    assert sound_key("Sophia") == "SF"
    assert sound_key("Sofia") == "SF"
    assert sound_key("Better") == "BTR"
    assert sound_key("Bettor") == "BTR"
    assert sound_key("Shell") == "XL"
    assert sound_key("About") == "ABT"
    assert sound_key("Coppola") == sound_key("Copola")
    assert sound_key("Knight") == sound_key("Night")
    assert sound_key("Catherine") == sound_key("Kathryn")
    assert sound_key("Geoff") == sound_key("Jeff")
    assert sound_key("Zoë") == sound_key("Zoe")
    assert sound_key("Shell") != sound_key("Sell")
    assert sound_key("1234") == ""
    assert sound_key("") == ""


def test_distance_and_common_words():
    assert distance("sophia", "sofia") == 2
    assert distance("kitten", "sitting") == 3
    assert distance("", "abc") == 3
    assert distance("same", "same") == 0
    assert is_common_word("Sofa")
    assert is_common_word("the")
    assert not is_common_word("Sophia")


# ------------------------------------------------------------------- replacements


def test_replacements_like_phone_text_replacement():
    rules = [("omw", "on my way")]
    assert apply_fixes("omw, see you soon", [], rules) == "on my way, see you soon"
    assert apply_fixes("Omw.", [], rules) == "On my way."
    assert apply_fixes("OMW", [], rules) == "On my way"
    assert apply_fixes("zomwz and omwhere", [], rules) == "zomwz and omwhere"


def test_replacement_spoken_phrase_and_symbols():
    rules = [("slash consolidate", "/consolidate")]
    assert apply_fixes("Slash consolidate.", [], rules) == "/consolidate."
    assert apply_fixes("run slash, consolidate now", [], rules) == "run /consolidate now"
    assert apply_fixes("please consolidate it", [], rules) == "please consolidate it"


def test_replacement_keeps_its_own_capitals():
    assert apply_fixes("Sophia called", [], [("Sophia", "Sofia")]) == "Sofia called"
    assert apply_fixes("my eye phone", [], [("eye phone", "iPhone")]) == "my iPhone"


def test_longest_replacement_first():
    rules = [("c sharp", "C#"), ("c sharp code", "C# source")]
    assert apply_fixes("write c sharp code", [], rules) == "write C# source"


def test_custom_words_with_symbols_are_matched_when_said():
    assert apply_fixes("Slash consolidate", ["/consolidate"]) == "/consolidate"
    assert apply_fixes("then slash consolidate the notes", ["/consolidate"]) == (
        "then /consolidate the notes"
    )
    assert apply_fixes("I'll consolidate later", ["/consolidate"]) == "I'll consolidate later"
    assert apply_fixes("built in c sharp", ["C#"]) == "built in C#"
    assert apply_fixes("a node dot js app", ["Node.js"]) == "a Node.js app"
    assert apply_fixes("set my underscore var", ["my_var"]) == "set my_var"
    assert apply_fixes("the dotnet runtime", [".NET"]) == "the dotnet runtime"
    assert apply_fixes("already /consolidate", ["/consolidate"]) == "already /consolidate"
