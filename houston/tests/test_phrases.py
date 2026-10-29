import pytest

from houston.phrases import classify, is_phantom


@pytest.mark.parametrize("text,name,kind,rest", [
    ("Hey Fred, ¿cuántos días dura un año en Marte?", "Fred", "wake", "cuántos días dura un año en Marte"),
    ("¡Hej Fred! ¡Hej Fred!", "Fred", "wake", ""),
    ("Oye Fred", "Fred", "wake", ""),
    ("¡Hola, Fred!", "Fred", "wake", ""),
    ("¿Hay Fred?", "Fred", "wake", ""),
    ("Hey Eva, ¿qué ves?", "Fred", "other", "Hey Eva, ¿qué ves?"),
    ("Hey Eva, ¿qué ves?", "Eva", "wake", "qué ves"),
    ("Le dije a Fred que sí", "Fred", "other", "Le dije a Fred que sí"),
    ("Gracias.", "Fred", "close", ""),
    ("Gracias, Fred.", "Fred", "close", ""),
    ("Thanks!", "Eva", "close", ""),
    ("Muchas gracias Eva", "Eva", "close", ""),
    ("Gracias, ¿y en Júpiter?", "Fred", "other", "Gracias, ¿y en Júpiter?"),
    ("Fred, descansa", "Fred", "pause", ""),
    ("Fred, despierta", "Fred", "resume", ""),
    ("Hey Heva", "Eva", "wake", ""),
    ("Hey Zed, hola", "Zed", "wake", "hola"),
    ("Gracias, Eva", "Fred", "other", "Gracias, Eva"),
])
def test_classify(text, name, kind, rest):
    heard = classify(text, name)
    assert heard.kind == kind
    assert heard.rest == rest


@pytest.mark.parametrize("text", [
    "Gracias por ver el video.", "¡Gracias por ver el video!",
    "Suscríbete al canal", "Subtítulos por la comunidad",
])
def test_phantoms(text):
    assert is_phantom(text)


def test_plain_gracias_is_not_phantom():
    # the close rule needs it; Task 7 gates it on energy
    assert not is_phantom("Gracias.")
