"""Cut streamed model text into speakable sentences: no markdown, no emoji, not too long."""
from __future__ import annotations

import re

MAX_WORDS = 25
MIN_WORDS = 6  # don't cut a long sentence into a stub like "Bueno,"

_MARKUP = re.compile(r"[*#`]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")
_END = re.compile(r"[.!?…]+(?=\s)|[.!?…]+$|\n")
_LIST_NUMBER = re.compile(r"\s*\d+")  # "1" before the "." of a numbered list item
_BULLET = re.compile(r"^\s*(?:[-•+]|\d+[.)])\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?…])")
_WORDY = re.compile(r"\w")


def _clean(piece: str) -> str:
    piece = _BULLET.sub("", piece)
    piece = _SPACE_BEFORE_PUNCT.sub(r"\1", " ".join(piece.split()))
    return piece if _WORDY.search(piece) else ""


def _comma_cut(text: str) -> int | None:
    """Where to cut a sentence over MAX_WORDS: after the last comma that keeps the first part
    within MAX_WORDS, else after the first comma past it. None if it's short or has no comma yet."""
    if len(text.split()) <= MAX_WORDS:
        return None
    best = None
    for m in re.finditer(",", text):
        words = len(text[:m.end()].split())
        if MIN_WORDS <= words <= MAX_WORDS:
            best = m.end()
        elif words > MAX_WORDS:
            return best or m.end()
    return best


def _split_long(sentence: str) -> list[str]:
    parts = []
    while (cut := _comma_cut(sentence)) is not None:
        parts.append(sentence[:cut])
        sentence = sentence[cut:]
    return parts + [sentence]


class Splitter:
    def __init__(self):
        self._buf = ""

    def push(self, text: str) -> list[str]:
        """Add streamed text; return the sentences it completed."""
        self._buf += _EMOJI.sub("", _MARKUP.sub("", text))
        out = []
        while (cut := self._sentence_end()) is not None:
            out += self._emit(self._buf[:cut])
            self._buf = self._buf[cut:]
        while (cut := _comma_cut(self._buf)) is not None:
            out += self._emit(self._buf[:cut])
            self._buf = self._buf[cut:]
        return out

    def flush(self) -> list[str]:
        """End of stream: whatever is left, even without final punctuation."""
        rest, self._buf = self._buf, ""
        return self._emit(rest)

    def _emit(self, raw: str) -> list[str]:
        return [p for p in (_clean(s) for s in _split_long(raw)) if p]

    def _sentence_end(self) -> int | None:
        buf = self._buf
        for m in _END.finditer(buf):
            if m.group() != "\n":
                line = buf[buf.rfind("\n", 0, m.start()) + 1:m.start()]
                if _LIST_NUMBER.fullmatch(line):
                    continue  # "1. " opens a list item, it doesn't end a sentence
                if m.end() == len(buf) and m.group() == "." and buf[m.start() - 1:m.start()].isdigit():
                    return None  # "45." may still become "45.22"
            return m.end()
        return None
