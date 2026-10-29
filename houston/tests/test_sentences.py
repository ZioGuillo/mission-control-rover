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


def test_long_sentence_never_cut_inside_decimal():
    text = ("Perseverance ya recorrió en total " + " ".join(["muchos"] * 22)
            + " kilómetros que suman 45,22 kilómetros, y sigue rodando por el cráter Jezero.")
    s = Splitter()
    out = s.push(text) + s.flush()
    assert all(not p.endswith("45,") for p in out)
    assert any("45,22 kilómetros," in p for p in out)


def test_decimal_comma_streamed_is_not_cut():
    s = Splitter()
    out = []
    for w in ["uno"] * 26 + ["45,", "22", "km,", "fin."]:
        out += s.push(w + ("" if w == "45," else " "))
    assert out + s.flush() == [" ".join(["uno"] * 26) + " 45,22 km,", "fin."]


def test_sentence_starting_with_a_year_is_kept():
    s = Splitter()
    assert s.push("2022. Ese es el sol de hoy. ") == ["2022.", "Ese es el sol de hoy."]


def test_number_mid_line_is_not_a_bullet():
    s = Splitter()
    assert s.push("Vamos bien. 2. Sigue. ") == ["Vamos bien.", "2.", "Sigue."]


def test_abbreviations_do_not_end_sentences():
    s = Splitter()
    assert s.push("El Sr. Pérez vive en EE. UU. desde hace años. ") == [
        "El Sr. Pérez vive en EE. UU. desde hace años."]
    assert s.push("Pesa aprox. diez kilos, p. ej. como un perro. ") == [
        "Pesa aprox. diez kilos, p. ej. como un perro."]


def test_italic_underscores_removed():
    s = Splitter()
    assert s.push("Es _muy_ rojo. ") == ["Es muy rojo."]


def test_markdown_link_becomes_its_text():
    s = Splitter()
    assert s.push("Mira la [página de la NASA](https://science.nasa.gov/mars/). ") == [
        "Mira la página de la NASA."]


def test_think_block_never_spoken_even_across_chunks():
    s = Splitter()
    assert s.push("<think>Debo pensar. Mucho. ") == []
    assert s.push("Más.</think>Hola base. ") == ["Hola base."]
    assert s.push("<think>otra vez. ") == []
    assert s.flush() == []
