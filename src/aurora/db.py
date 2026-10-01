"""Armazenamento do condomínio em SQLite.

Os arquivos de `dados/` são só o estado inicial: `restaurar_dados()` copia
o conteúdo deles para o banco, e daí em diante todas as leituras e gravações
acontecem aqui.

Garantia 5 (dois moradores, uma reserva): a exclusividade não depende de
uma conferência prévia. O índice único parcial `ux_reserva_ativa` impede,
dentro do próprio SQLite e no instante do INSERT, que existam duas reservas
ativas para a mesma área na mesma data. Quem chega depois recebe
`IntegrityError` e a reserva é recusada.

Regra de negócio 5 (código nunca repete): todo código emitido vai para a
tabela `codigos_emitidos`, que tem chave primária no código e nunca é
apagada, nem pela restauração. Uma reserva cancelada continua ocupando o seu
código.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import string
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (
    numero  TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS areas (
    id   TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo     TEXT PRIMARY KEY,
    emitido_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservas (
    codigo       TEXT PRIMARY KEY REFERENCES codigos_emitidos(codigo),
    apartamento  TEXT NOT NULL,
    area         TEXT NOT NULL,
    data         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('ativa', 'cancelada')),
    criada_em    TEXT NOT NULL,
    cancelada_em TEXT
);

-- Garantia 5: no máximo uma reserva ATIVA por área e data, garantido pelo banco.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa
    ON reservas (area, data) WHERE status = 'ativa';

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL,
    criado_em   TEXT NOT NULL
);
"""


def _agora() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def conexao() -> Iterator[sqlite3.Connection]:
    config.VAR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.BANCO_CONDOMINIO, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
    finally:
        con.close()


def _ler_json(nome: str) -> list[dict]:
    return json.loads((config.DADOS / nome).read_text(encoding="utf-8"))


def criar_schema() -> None:
    with conexao() as con:
        con.executescript(SCHEMA)


def restaurar_dados() -> None:
    """Volta apartamentos, áreas, reservas e visitantes ao estado de `dados/`."""
    criar_schema()
    with conexao() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM reservas")
            con.execute("DELETE FROM visitantes")
            con.execute("DELETE FROM apartamentos")
            con.execute("DELETE FROM areas")
            con.executemany(
                "INSERT INTO apartamentos (numero, morador) VALUES (:numero, :morador)",
                _ler_json("apartamentos.json"),
            )
            con.executemany(
                "INSERT INTO areas (id, nome, taxa) VALUES (:id, :nome, :taxa)",
                _ler_json("areas.json"),
            )
            agora = _agora()
            for r in _ler_json("reservas.json"):
                con.execute(
                    "INSERT OR IGNORE INTO codigos_emitidos (codigo, emitido_em) VALUES (?, ?)",
                    (r["codigo"], agora),
                )
                con.execute(
                    "INSERT INTO reservas (codigo, apartamento, area, data, status, criada_em)"
                    " VALUES (?, ?, ?, ?, 'ativa', ?)",
                    (r["codigo"], r["apartamento"], r["area"], r["data"], agora),
                )
            con.executemany(
                "INSERT INTO visitantes (apartamento, nome, data, criado_em)"
                " VALUES (:apartamento, :nome, :data, :criado_em)",
                [{**v, "criado_em": agora} for v in _ler_json("visitantes.json")],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def garantir_banco() -> None:
    """Cria o banco com os dados iniciais se ele ainda não existir."""
    criar_schema()
    with conexao() as con:
        vazio = con.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 0
    if vazio:
        restaurar_dados()


# --------------------------------------------------------------------------
# Leituras
# --------------------------------------------------------------------------


def apartamento_existe(numero: str) -> bool:
    with conexao() as con:
        return (
            con.execute(
                "SELECT 1 FROM apartamentos WHERE numero = ?", (numero,)
            ).fetchone()
            is not None
        )


def listar_areas() -> list[dict]:
    with conexao() as con:
        rows = con.execute("SELECT id, nome, taxa FROM areas ORDER BY nome").fetchall()
    return [dict(r) for r in rows]


def data_ocupada(area_id: str, data: str) -> bool:
    """Diz apenas se existe reserva ativa. Nunca devolve de quem é."""
    with conexao() as con:
        row = con.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area_id, data),
        ).fetchone()
    return row is not None


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        rows = con.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        rows = con.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Gravações
# --------------------------------------------------------------------------


@dataclass
class ResultadoReserva:
    criada: bool
    codigo: str | None = None


_ALFABETO = string.ascii_uppercase + string.digits


def _novo_codigo() -> str:
    return "RSV-" + "".join(secrets.choice(_ALFABETO) for _ in range(6))


def criar_reserva(apartamento: str, area_id: str, data: str) -> ResultadoReserva:
    """Grava a reserva numa única transação.

    A exclusividade vem do índice `ux_reserva_ativa`: se outra reserva ativa
    para a mesma área e data entrar antes (mesmo entre uma conferência e esta
    gravação), o INSERT falha com IntegrityError e nada é gravado.
    """
    with conexao() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            agora = _agora()
            while True:
                codigo = _novo_codigo()
                try:
                    con.execute(
                        "INSERT INTO codigos_emitidos (codigo, emitido_em) VALUES (?, ?)",
                        (codigo, agora),
                    )
                    break
                except sqlite3.IntegrityError:
                    continue  # código já emitido algum dia: sorteia outro
            con.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, status, criada_em)"
                " VALUES (?, ?, ?, ?, 'ativa', ?)",
                (codigo, apartamento, area_id, data, agora),
            )
            con.execute("COMMIT")
            return ResultadoReserva(criada=True, codigo=codigo)
        except sqlite3.IntegrityError:
            con.execute("ROLLBACK")
            return ResultadoReserva(criada=False)
        except Exception:
            con.execute("ROLLBACK")
            raise


def cancelar_reserva(apartamento: str, area_id: str, data: str) -> str | None:
    """Cancela a reserva ativa do PRÓPRIO apartamento. Devolve o código ou None."""
    with conexao() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute(
                "SELECT codigo FROM reservas"
                " WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'",
                (apartamento, area_id, data),
            ).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                return None
            con.execute(
                "UPDATE reservas SET status = 'cancelada', cancelada_em = ? WHERE codigo = ?",
                (_agora(), row["codigo"]),
            )
            con.execute("COMMIT")
            return row["codigo"]
        except Exception:
            con.execute("ROLLBACK")
            raise


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with conexao() as con:
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data, criado_em) VALUES (?, ?, ?, ?)",
            (apartamento, nome, data, _agora()),
        )
