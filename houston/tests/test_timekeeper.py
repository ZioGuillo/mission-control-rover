import pytest

from datetime import datetime

from houston.timekeeper import START, STOP, clock_time, duration


@pytest.mark.parametrize("seconds, said", [
    (131.4, "dos minutos y once segundos"), (60, "un minuto"), (1, "un segundo"), (0, "cero segundos"),
    (21 * 60 + 31, "veintiún minutos y treinta y un segundos"), (45, "cuarenta y cinco segundos"),
    (3600 + 120 + 5, "una hora, dos minutos y cinco segundos"), (2 * 3600, "dos horas")])
def test_durations_are_spoken_in_words(seconds, said):
    assert duration(seconds) == said


@pytest.mark.parametrize("text", ["cronométrame", "Bentley, cronometra mi charla", "toma el tiempo",
                                  "empieza el cronómetro", "mide mi tiempo", "activa el cronómetro"])
def test_stopwatch_start(text):
    assert START.search(text) and not STOP.search(text)


@pytest.mark.parametrize("text", ["para el cronómetro", "detén el tiempo", "ya terminé", "Terminé",
                                  "pausa el cronómetro"])
def test_stopwatch_stop(text):
    assert STOP.search(text)


@pytest.mark.parametrize("text", ["¿Cuánto tarda la luz a Marte?", "¿Qué hora es?", "¿Cuánto llevo?"])
def test_plain_questions_dont_touch_the_stopwatch(text):
    assert not START.search(text) and not STOP.search(text)


@pytest.mark.parametrize("hour, minute, said", [
    (13, 19, "la una y diecinueve de la tarde"), (9, 0, "las nueve en punto de la mañana"),
    (0, 30, "las doce y media de la madrugada"), (20, 15, "las ocho y cuarto de la noche"),
    (12, 45, "las doce y cuarenta y cinco de la tarde")])
def test_clock_time_in_words(hour, minute, said):
    assert clock_time(datetime(2026, 10, 29, hour, minute)) == said
