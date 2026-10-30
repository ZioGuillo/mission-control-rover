"""Story sound effects: the model writes [rugido] where a roar goes; Houston plays the sound."""
from __future__ import annotations

import re
from pathlib import Path

AUDIO = Path(__file__).parent / "audio"
SOUNDS = {name: AUDIO / "effects" / f"{name}.wav" for name in ("rugido", "alas", "fuego", "trueno", "magia")}
SOUNDS["ladrido"] = AUDIO / "barks_soft.wav"
MARK = "\x07"  # starts a sentence that is a sound, not words; never spoken or kept in history
_TAG = re.compile(r"\[\s*([^\]\s]+)\s*\]")


def split(sentence: str) -> list[str]:
    """ "El dragón abrió la boca [rugido] y tembló." -> words, MARK+"rugido", words.
    Unknown tags are dropped, never read aloud."""
    out: list[str] = []
    pos = 0
    for m in _TAG.finditer(sentence):
        out.append(sentence[pos:m.start()])
        name = m.group(1).lower()
        if name in SOUNDS:
            out.append(MARK + name)
        pos = m.end()
    out.append(sentence[pos:])
    return [p if p.startswith(MARK) else " ".join(p.split()) for p in out
            if p.startswith(MARK) or re.search(r"[^\W_]", p)]


def sound(item: str) -> Path | None:
    """The file for a MARK sentence, else None."""
    return SOUNDS.get(item[len(MARK):]) if item.startswith(MARK) else None
