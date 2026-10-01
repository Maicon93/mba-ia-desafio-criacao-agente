"""Leitura das confirmações pendentes a partir dos eventos da sessão.

Quando uma tool chama `tool_context.request_confirmation(...)`, o ADK grava na
sessão um evento com uma function call `adk_request_confirmation`. Ela fica
pendente até a sessão receber uma function response com o mesmo `id`, que só
a rota `POST /sessoes/{id}/confirmacoes` envia.

A fonte da verdade é a própria sessão persistida: uma confirmação já respondida
tem a function response gravada e deixa de ser pendente, então reenviar o mesmo
`id` cai no 409 (Garantia 1), inclusive depois de reiniciar a API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.adk.events import Event

PEDIDO_CONFIRMACAO = "adk_request_confirmation"


@dataclass
class Pendente:
    id: str
    acao: str
    detalhes: dict[str, Any]
    invocation_id: str
    chamada_original_id: str
    autor: str  # agente que pediu a confirmação (quem deve receber a resposta)


def pendentes(eventos: list[Event]) -> list[Pendente]:
    respondidas: set[str] = set()
    pedidos: list[Pendente] = []
    for evento in eventos:
        for resposta in evento.get_function_responses():
            if resposta.name == PEDIDO_CONFIRMACAO and resposta.id:
                respondidas.add(resposta.id)
        for chamada in evento.get_function_calls():
            if chamada.name != PEDIDO_CONFIRMACAO or not chamada.id:
                continue
            args = chamada.args or {}
            original = args.get("originalFunctionCall") or {}
            confirmacao = args.get("toolConfirmation") or {}
            detalhes = confirmacao.get("payload") or original.get("args") or {}
            pedidos.append(
                Pendente(
                    id=chamada.id,
                    acao=original.get("name", "acao"),
                    detalhes=dict(detalhes),
                    invocation_id=evento.invocation_id,
                    chamada_original_id=original.get("id", ""),
                    autor=evento.author,
                )
            )
    return [p for p in pedidos if p.id not in respondidas]


def tool_reexecutada(eventos: list[Event], pendente: Pendente) -> bool:
    """Diz se, depois da resposta à confirmação, a tool original rodou de novo.

    Serve para detectar a "falha silenciosa": a rota aceita a resposta, mas a
    retomada cai no agente errado e a tool nunca é executada.
    """
    respondeu = False
    for evento in eventos:
        for resposta in evento.get_function_responses():
            if resposta.name == PEDIDO_CONFIRMACAO and resposta.id == pendente.id:
                respondeu = True
            elif respondeu and resposta.id == pendente.chamada_original_id:
                return True
    return False


def como_json(lista: list[Pendente]) -> list[dict[str, Any]]:
    return [{"id": p.id, "acao": p.acao, "detalhes": p.detalhes} for p in lista]
