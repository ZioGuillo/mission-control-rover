import pytest

from houston import effects
from houston.brain import is_story


def test_tags_become_sounds_and_are_never_read():
    assert effects.split("El dragón abrió la boca [rugido] y todos temblaron.") == [
        "El dragón abrió la boca", effects.MARK + "rugido", "y todos temblaron."]
    assert effects.split("[ Alas ]") == [effects.MARK + "alas"]
    assert effects.split("Y entonces [explosión] pasó.") == ["Y entonces", "pasó."]  # unknown: dropped
    assert effects.split("Sin efectos.") == ["Sin efectos."]


def test_every_sound_file_exists():
    for name, path in effects.SOUNDS.items():
        assert path.is_file(), name
    assert effects.sound(effects.MARK + "trueno") == effects.SOUNDS["trueno"]
    assert effects.sound("trueno") is None


def chat(*turns):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(turns)]


@pytest.mark.parametrize("history", [
    chat("Cuéntale una historia de dragones a Lucca"),
    chat("Bentley, cuéntame un cuento"),
    chat("Pregúntale a Lucca si quiere oír una historia de dragones",
         "Hey Lucca, ¿quieres oír una historia de dragones?", "¡Sí!"),
    chat("hola", "¿Te cuento un cuento de dragones?", "Sí, por favor")])
def test_story_requests(history):
    assert is_story(history)


@pytest.mark.parametrize("history", [
    chat("Pregúntale a Lucca si quiere oír una historia de dragones"),
    chat("¿Cuál es la historia de Marte?"),
    chat("¿Marte tiene lunas?", "Sí, dos: Fobos y Deimos. ¿Quieres saber más?", "Sí"),
    chat("Cuéntale una historia", "Había una vez...", "¿Cuántas lunas tiene Marte?")])
def test_not_story_requests(history):
    assert not is_story(history)
