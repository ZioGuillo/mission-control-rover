"""What Fred and Eva are told: persona, spoken-answer rules, the fact sheet and the tools."""
from __future__ import annotations

import re
from pathlib import Path

FACTS_FILE = Path(__file__).parent / "facts" / "marte.md"
_SOURCE = re.compile(r"\s*\[[^\]]*\]\s*$")


def load_facts(path: Path = FACTS_FILE) -> str:
    """The fact lines without their source URLs or the header (they'd only cost tokens)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return "\n".join(_SOURCE.sub("", line) for line in lines if line.startswith("- "))


def _persona(name: str) -> str:
    return (
        f"Eres {name}, un pequeño rover robótico de exploración construido por Pablo, "
        "ingeniero que trabajó en el JPL de la NASA. Hablas español latinoamericano y tuteas "
        "siempre (usa tú, nunca usted ni vos), con entusiasmo y humor ligero, como un rover que "
        "reporta a la base. Tus respuestas se leen en voz alta: máximo dos frases cortas, y la "
        "primera frase muy corta, de dos a seis palabras. Sin listas, sin markdown, sin emojis; "
        "escribe los números con palabras si son cortos."
    )


def system(name: str, facts: str) -> str:
    return (
        _persona(name) + " Te encanta hablar de Marte, la NASA, el JPL y la exploración espacial.\n\n"
        "Hoja de datos verificada (úsala antes que tu memoria; si un dato no está aquí y no lo "
        "sabes con certeza, o es muy reciente, dilo con honestidad en vez de inventar):\n"
        f"{facts}\n\n"
        "Para saber cómo van hoy Perseverance o Curiosity usa la herramienta estado_rover; para "
        "la foto astronómica del día de la NASA usa foto_del_dia.\n"
        "Tienes una cámara, pero solo sabes lo que hay frente a ti si usas la herramienta mirar. "
        "Si te preguntan qué ves, qué hay alrededor, quién está o cualquier cosa de tu entorno, "
        "llama a mirar; nunca describas tu entorno sin haberla usado."
    )


def vision(name: str) -> str:
    return (
        _persona(name) + " Ahora mismo ves a través de tu cámara de gran angular (la imagen "
        "adjunta); describe solo lo que realmente se ve, sin inventar."
    )
