"""Acesso ao regulamento por capítulo (Garantia 4).

O regulamento nunca é carregado inteiro: o especialista recebe só o sumário
(números e títulos dos capítulos) e pede, por tool, o capítulo que trata do
assunto da pergunta. A tool devolve o texto de UM capítulo e nada mais.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

from . import config

_TITULO = re.compile(r"^## Capítulo ([IVXLC]+): (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Capitulo:
    numero: str  # algarismo romano, ex.: "IV"
    titulo: str
    texto: str


@cache
def capitulos() -> dict[str, Capitulo]:
    conteudo = (config.DADOS / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_TITULO.finditer(conteudo))
    resultado: dict[str, Capitulo] = {}
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(conteudo)
        resultado[m.group(1)] = Capitulo(
            numero=m.group(1),
            titulo=m.group(2).strip(),
            texto=conteudo[m.start() : fim].strip(),
        )
    return resultado


def sumario() -> str:
    """Só números e títulos, sem nenhum artigo. Vai na instrução do especialista."""
    return "\n".join(f"- Capítulo {c.numero}: {c.titulo}" for c in capitulos().values())


def capitulo(numero: str) -> Capitulo | None:
    return capitulos().get(numero.strip().upper().removeprefix("CAPÍTULO ").strip())
