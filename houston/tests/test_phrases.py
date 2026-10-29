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


@pytest.mark.parametrize("text,rest", [
    ("Hey Bentley, ¿qué ves?", "qué ves"),
    ("Hola Bently", ""),
    ("Oye Benley, ¿cuántas lunas tiene Marte?", "cuántas lunas tiene Marte"),
    ("Hey Bentli", ""),
])
def test_bentley_wakes(text, rest):
    heard = classify(text, "Bentley")
    assert heard.kind == "wake" and heard.rest == rest


def test_bentley_does_not_wake_on_fred():
    assert classify("Hey Fred, ¿qué ves?", "Bentley").kind == "other"


@pytest.mark.parametrize("text,rest", [
    ("Oye el Bentley.", ""),                       # Whisper adds an article
    ("Hoy el Bentley, ¿qué ves?", "qué ves"),
    ("Bentley, ¿te gusta Marte?", "te gusta Marte"),  # the name alone opening the sentence
    ("Bently, ¿qué hora es?", "qué hora es"),
])
def test_bentley_wake_tolerates_whisper_spellings(text, rest):
    heard = classify(text, "Bentley")
    assert heard.kind == "wake" and heard.rest == rest


def test_name_alone_does_not_wake_common_names():
    # "Eva" is also a person's name: only rare rover names wake without a greeting.
    assert classify("Eva, ven a comer", "Eva").kind == "other"
    assert classify("Fred, pásame eso", "Fred").kind == "other"
    assert classify("Le dije a Bentley que sí", "Bentley").kind == "other"


@pytest.mark.parametrize("text", ["Hey Benli", "Oye Benli, ¿qué ves?", "Benli, ¿te gusta Marte?"])
def test_benli_is_bentley(text):
    assert classify(text, "Bentley").kind == "wake"


@pytest.mark.parametrize("text,kind", [
    ("Ok, Bentley, descansa", "pause"),      # live: answered as a question instead
    ("Bueno Bentley, despierta", "resume"),
    ("Ok, gracias", "close"),
    ("Ok, Bentley, ¿y Júpiter?", "other"),   # not a command
])
def test_commands_tolerate_a_leading_ok(text, kind):
    assert classify(text, "Bentley").kind == kind
