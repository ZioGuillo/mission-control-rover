from houston.sentences import Splitter


def test_complete_sentence_out_partial_kept():
    s = Splitter()
    assert s.push("Un año en Marte dura 687 días. ¡Ca") == ["Un año en Marte dura 687 días."]
    assert s.push("si el doble!") == ["¡Casi el doble!"]
    assert s.flush() == []


def test_markdown_stripped_and_unpunctuated_tail_flushed():
    s = Splitter()
    assert s.push("**Hola** base") == []
    assert s.flush() == ["Hola base"]


def test_markdown_around_punctuation_still_splits():
    s = Splitter()
    assert s.push("**Claro.** Marte es `rojo`. ") == ["Claro.", "Marte es rojo."]


def test_emoji_removed():
    s = Splitter()
    assert s.push("¡Hola, base! 🚀🔴 Todo bien 👍🏽.") == ["¡Hola, base!", "Todo bien."]
    assert s.push("✨ ❤️ ") == []
    assert s.flush() == []


def test_list_bullets_and_headings_removed():
    s = Splitter()
    out = s.push("# Datos\n- Marte es rojo\n1. Tiene dos lunas.\n")
    assert out + s.flush() == ["Datos", "Marte es rojo", "Tiene dos lunas."]


def test_decimal_number_at_chunk_end_waits():
    s = Splitter()
    assert s.push("Recorrió 45.") == []
    assert s.push("22 kilómetros. ") == ["Recorrió 45.22 kilómetros."]


def test_punctuation_only_pieces_dropped():
    s = Splitter()
    assert s.push("Bueno... . ") == ["Bueno..."]
    assert s.flush() == []


def test_long_sentence_split_at_comma():
    first = " ".join(["palabra"] * 20) + ","
    second = " ".join(["otra"] * 20) + "."
    s = Splitter()
    out = s.push(first + " " + second + " ") + s.flush()
    assert out == [first, second]


def test_long_sentence_split_while_streaming():
    s = Splitter()
    words = ["uno"] * 30
    words[27] += ","
    out = []
    for w in words + ["fin"]:
        out += s.push(w + " ")
    assert out == [" ".join(words[:28])]
    assert s.flush() == ["uno uno fin"]


def test_long_sentence_without_comma_kept_whole():
    text = " ".join(["palabra"] * 40) + "."
    s = Splitter()
    assert s.push(text) == [text]
