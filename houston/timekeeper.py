"""Everything about time that Houston measures for a rover, so it never invents a duration.

Live 2026-10-29: asked how long his talk demo took, Bentley said "tres horas", then "54 minutos";
with no demo at all he said "unos tres minutos". Every answer now carries what was really measured:
the date and time, this conversation, the last answer, the last talk demo, and a stopwatch Pablo
starts and stops by voice to time his rehearsals. The exact ones (what time it is, the stopwatch)
Houston says itself: gpt-oss read 13:19 as "trece mil diecinueve" and repeated an old stopwatch time.
"""
from __future__ import annotations

import re
from datetime import datetime

from houston.brain import DEMO

# "Bentley, cronométrame", "toma el tiempo", "empieza el cronómetro"
START = re.compile(r"\b(cronom[eé]tr(a|ame|ar|o)\w*|toma(me)? el tiempo|mide (el|mi) tiempo|"
                   r"(empieza|inicia|arranca|activa|prende)\w* (el |a contar el )?(cron[oó]metro|tiempo))\b", re.I)
# "para el cronómetro", "detén el tiempo", "ya terminé"
STOP = re.compile(r"\b((para|det[eé]n|pausa|termina|apaga|frena)\w* (el )?(cron[oó]metro|tiempo)|(ya )?termin[eé])\b",
                  re.I)

# "¿Cuánto llevo?", "¿cuánto va?", "¿cuánto marca el cronómetro?" (only while it runs)
ELAPSED = re.compile(r"\bcu[aá]nto (tiempo )?(llevo|llevamos|va|lleva|marca)\b", re.I)
TIME = re.compile(r"\bqu[eé] hora (es|son|tienes)\b", re.I)

_DAYS = "lunes martes miércoles jueves viernes sábado domingo".split()
_MONTHS = ("enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre "
           "diciembre").split()
_UNITS = ("cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince "
          "dieciséis diecisiete dieciocho diecinueve veinte veintiuno veintidós veintitrés veinticuatro "
          "veinticinco veintiséis veintisiete veintiocho veintinueve").split()
_TENS = {3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta", 7: "setenta", 8: "ochenta", 9: "noventa"}


def words(n: int) -> str:
    """0..99 in Spanish words, before a noun ("veintiún minutos", "un segundo")."""
    word = _UNITS[n] if n < 30 else _TENS[n // 10] + (f" y {_UNITS[n % 10]}" if n % 10 else "")
    return word[:-3] + "ún" if word.endswith("iuno") else word[:-1] if word.endswith("uno") else word


def duration(seconds: float) -> str:
    h, rest = divmod(round(seconds), 3600)
    m, s = divmod(rest, 60)
    parts = [f"{'una' if (n, unit) == (1, 'hora') else words(n)} {unit}{'s' * (n != 1)}"
             for n, unit in ((h, "hora"), (m, "minuto")) if n]
    if s or not parts:
        parts.append(f"{words(s)} segundo{'s' * (s != 1)}")
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " y " + parts[-1]


def clock_time(wall: datetime) -> str:
    """ "la una y diecinueve de la tarde": digits like 13:19 were read "trece mil diecinueve"."""
    h12 = wall.hour % 12 or 12
    hour = "la una" if h12 == 1 else f"las {words(h12)}"
    minutes = {0: "en punto", 15: "y cuarto", 30: "y media"}.get(wall.minute, f"y {words(wall.minute)}")
    part = ("de la madrugada" if wall.hour < 6 else "de la mañana" if wall.hour < 12
            else "de la tarde" if wall.hour < 19 else "de la noche")
    return f"{hour} {minutes} {part}"


class Timekeeper:
    def __init__(self) -> None:
        self.conversation_start: float | None = None
        self.last_answer: float | None = None  # seconds, from the question to the last word
        self.demo_parts: list[float] = []
        self.stopwatch_start: float | None = None
        self.stopwatch_last: float | None = None  # what it read when it was last stopped

    def question(self, text: str, now: float, wall: datetime, name: str) -> str | None:
        """A question arrived: start the conversation clock, a new demo, or the stopwatch.
        Returns the exact reply for Houston to say itself, or None for the model."""
        if self.conversation_start is None:
            self.conversation_start = now
        if DEMO.search(text):
            self.demo_parts = []
        if STOP.search(text):
            if self.stopwatch_start is None:
                return "El cronómetro no estaba corriendo."
            self.stopwatch_last = now - self.stopwatch_start
            self.stopwatch_start = None
            return f"Cronómetro parado: {duration(self.stopwatch_last)}."
        if START.search(text):
            self.stopwatch_start = now
            return f"Cronómetro corriendo. Cuando termines, dime: {name}, para el cronómetro."
        if ELAPSED.search(text) and self.stopwatch_start is not None:
            return f"Llevas {duration(now - self.stopwatch_start)}."
        if TIME.search(text):
            return f"Son {clock_time(wall)}."
        return None

    def answered(self, seconds: float, demo: bool) -> None:
        self.last_answer = seconds
        if demo:
            self.demo_parts.append(seconds)

    def end_conversation(self) -> None:
        self.conversation_start = None

    def note(self, now: float, wall: datetime) -> str:
        """The measured times, for the system prompt: the only times the rover may give."""
        lines = [f"Hoy es {_DAYS[wall.weekday()]} {wall.day} de {_MONTHS[wall.month - 1]} de {wall.year} y "
                 f"son {clock_time(wall)}, hora de California."]
        if self.conversation_start is not None:
            lines.append(f"Esta conversación empezó hace {duration(now - self.conversation_start)}.")
        if self.last_answer is not None:
            lines.append(f"Tu respuesta anterior duró {duration(self.last_answer)}, desde la pregunta "
                         "hasta tu última palabra.")
        if self.demo_parts:
            n = len(self.demo_parts)
            each = ", ".join(duration(p) for p in self.demo_parts)
            lines.append(f"Tú ya diste la charla en tu demo: tuvo {'una' if n == 1 else words(n)} "
                         f"parte{'s' * (n != 1)} ({each}) y a ti te tomó en total "
                         f"{duration(sum(self.demo_parts))}. Si te preguntan cuánto tardaste tú en dar la "
                         "charla, la demo o todas las partes, la respuesta es esa. Los cuarenta y cuatro "
                         "minutos de la hoja son lo que dura la charla cuando la da Pablo, no lo que tardaste tú.")
        else:
            lines.append("No has dado ninguna demo de la charla desde que Houston arrancó.")
        if self.stopwatch_start is not None:
            lines.append(f"El cronómetro de Pablo está corriendo: lleva {duration(now - self.stopwatch_start)}.")
        elif self.stopwatch_last is not None:
            lines.append(f"El cronómetro de Pablo está parado; la última vez marcó {duration(self.stopwatch_last)}.")
        else:
            lines.append("El cronómetro de Pablo no se ha usado; arranca cuando él te dice \"cronométrame\".")
        return ("\n\nTiempos medidos por Houston ahora mismo (reales y más nuevos que cualquier número de la "
                "conversación; para cualquier pregunta de tiempo usa solo estos, y si lo que te preguntan "
                "no está aquí, di que no lo mediste):\n" + "\n".join(lines))
