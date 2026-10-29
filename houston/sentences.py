"""Cut streamed model text into speakable sentences: no markdown, no emoji, not too long."""
from __future__ import annotations

import re

MAX_WORDS = 25
MIN_WORDS = 6  # don't cut a long sentence into a stub like "Bueno,"

_MARKUP = re.compile(r"[*#`_]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")
_THINK = re.compile(r"(?s)<think>.*?</think>")
_END = re.compile(r"[.!?…]+(?=\s)|[.!?…]+$|\n")
_COMMA = re.compile(r",(?=\s)")  # not "45,22"
_ABBREV = {"sr", "sra", "srta", "dr", "dra", "ee", "uu", "aprox", "etc", "ej", "p", "ud", "uds", "núm"}
_WORD_BEFORE = re.compile(r"(\w+)$")
_LIST_NUMBER = re.compile(r"\s*\d{1,2}")  # "1" before the "." of a numbered list item
_BULLET = re.compile(r"^\s*(?:[-•+]|\d{1,2}[.)])\s+")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?…])")
_WORDY = re.compile(r"[^\W_]")


def _clean(piece: str, line_start: bool) -> str:
    if line_start:
        piece = _BULLET.sub("", piece)
    piece = _LINK.sub(r"\1", piece)
    piece = _SPACE_BEFORE_PUNCT.sub(r"\1", " ".join(piece.split()))
    return piece if _WORDY.search(piece) else ""


def _comma_cut(text: str) -> int | None:
    """Where to cut a sentence over MAX_WORDS: after the last comma that keeps the first part
    within MAX_WORDS, else after the first comma past it. None if it's short or has no comma yet."""
    if len(text.split()) <= MAX_WORDS:
        return None
    best = None
    for m in _COMMA.finditer(text):
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
        self._line_start = True  # the buffer begins a line (stream start or after a newline)

    def push(self, text: str) -> list[str]:
        """Add streamed text; return the sentences it completed."""
        self._buf = _THINK.sub("", self._buf + _EMOJI.sub("", _MARKUP.sub("", text)))
        out = []
        while (cut := self._sentence_end()) is not None:
            out += self._take(cut)
        while (cut := _comma_cut(self._visible())) is not None:
            out += self._take(cut)
        return out

    def flush(self) -> list[str]:
        """End of stream: whatever is left, even without final punctuation."""
        out = self._take(len(self._visible()))
        self._buf, self._line_start = "", True
        return out

    def _visible(self) -> str:
        """The buffer up to an unfinished <think> block (never spoken)."""
        open_ = self._buf.find("<think>")
        return self._buf if open_ < 0 else self._buf[:open_]

    def _take(self, cut: int) -> list[str]:
        raw, self._buf = self._buf[:cut], self._buf[cut:]
        lead = raw[:len(raw) - len(raw.lstrip())]
        line_start = self._line_start or "\n" in lead
        self._line_start = raw.endswith("\n")
        parts = _split_long(raw)
        return [p for i, s in enumerate(parts) if (p := _clean(s, line_start and i == 0))]

    def _sentence_end(self) -> int | None:
        text = self._visible()
        for m in _END.finditer(text):
            if m.group() == "\n":
                return m.end()
            nl = text.rfind("\n", 0, m.start())
            if (nl >= 0 or self._line_start) and _LIST_NUMBER.fullmatch(text[nl + 1:m.start()]):
                continue  # "1. " opens a list item, it doesn't end a sentence
            if m.group() == ".":
                before = _WORD_BEFORE.search(text, 0, m.start())
                if before and before.group(1).lower() in _ABBREV:
                    continue  # "Sr.", "EE. UU.", "p. ej."
                if m.end() == len(text) and before and before.group(1)[-1].isdigit():
                    return None  # "45." may still become "45.22"
            return m.end()
        return None
