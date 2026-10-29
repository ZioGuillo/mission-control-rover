"""Pure phrase matching: wake words, close/pause/resume, Whisper phantoms."""
from __future__ import annotations

import re
from typing import Literal, NamedTuple

Kind = Literal["wake", "close", "pause", "resume", "other"]


class Heard(NamedTuple):
    kind: Kind
    rest: str


# Spellings Whisper produces for each rover name. Unknown names match literally.
_VARIANTS = {
    "fred": ["fred", "fret", "frey", "freddy", "fredi"],
    "eva": ["eva", "heva", "iva"],
    "bentley": ["bentley", "bently", "benley", "bentli", "bentlee", "ventley", "bendley", "vently",
                "benli", "benlee", "venli"],  # Pablo also says "Benli"
}
# Rare words in Spanish speech: the name alone opening a sentence ("Bentley, ¿qué ves?") wakes too.
# Not Fred/Eva — people are called that ("Eva, ven a comer").
_STANDALONE = {"bentley"}
_PREFIX = r"hey|hei|hej|ey|ei|oye|hoy|jey|hay|ay|hola"  # "hoy": Whisper's spelling of "oye"
_PAD = " \t,.!¡?¿:;"
_PUNCT = r"[\s,.!¡?¿:;]*"
# A filler word may open a command: "Ok, Bentley, descansa", "Bueno, gracias".
_LEAD = rf"(?:(?:ok|okay|bueno|ya|oye|hey){_PUNCT})?"

# Whisper's stock hallucinations on silence/noise (YouTube outros in its training data).
# Plain "gracias" is deliberately absent: the close rule needs it.
_PHANTOM = re.compile(
    r"^\W*(gracias por ver el video|suscr[ií]bete al canal|subt[ií]tulos.*)\W*$", re.I)


def _name_pattern(name: str) -> str:
    variants = _VARIANTS.get(name.lower(), [name.lower()])
    return "(?:" + "|".join(re.escape(v) for v in variants) + ")"


def classify(text: str, name: str) -> Heard:
    text = text.strip()
    n = _name_pattern(name)

    # Whisper sometimes adds an article: "Oye el Bentley".
    wake = re.compile(rf"\b(?:{_PREFIX}){_PUNCT}(?:el\s+)?{n}\b{_PUNCT}", re.I)
    m = wake.search(text)
    if not m and name.lower() in _STANDALONE:
        wake = re.compile(rf"^{_PUNCT}{n}\b{_PUNCT}", re.I)
        m = wake.search(text)
    if m:
        # Strip repeated wakes ("Hej Fred! Hej Fred!") as well as the first.
        rest = wake.sub(" ", text[m.start():]).strip(_PAD + " ")
        return Heard("wake", rest)

    close = re.compile(
        rf"^{_PUNCT}{_LEAD}(?:muchas\s+)?(?:gracias|thanks|thank you)(?:{_PUNCT}{n})?{_PUNCT}$", re.I)
    if close.match(text):
        return Heard("close", "")

    for kind, word in (("pause", "descansa"), ("resume", "despierta")):
        cmd = re.compile(rf"^{_PUNCT}{_LEAD}(?:{n}{_PUNCT}{word}|{word}{_PUNCT}{n}){_PUNCT}$", re.I)
        if cmd.match(text):
            return Heard(kind, "")

    return Heard("other", text)


def is_phantom(text: str) -> bool:
    return bool(_PHANTOM.match(text))
