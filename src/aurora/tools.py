"""Tools dos especialistas.

Garantia 2 (cada sessão pertence a um apartamento): NENHUMA tool recebe o
apartamento como parâmetro. Todas leem `_apartamento_da_sessao(tool_context)`,
que vem do state gravado na criação da sessão pela API. O modelo não tem
como escolher outro apartamento, nem que o morador peça.

Garantia 1 (cobrança ou acesso só com confirmação): `reservar_area` (quando a
área tem taxa) e `autorizar_visitante` (sempre) chamam
`tool_context.request_confirmation(...)` e devolvem sem gravar nada. O ADK
interrompe a execução e só roda a tool de novo quando a API devolve a resposta
do morador pela rota de confirmações; aí `tool_context.tool_confirmation`
chega preenchido. Texto na conversa ("já confirmei") não preenche esse campo.
"""

from __future__ import annotations

from datetime import date

from google.adk.tools import ToolContext

from . import config, db, regulamento


def _apartamento_da_sessao(tool_context: ToolContext) -> str:
    apartamento = tool_context.state.get(config.STATE_APARTAMENTO)
    if not apartamento:
        # Não deveria acontecer: a API sempre grava o apartamento ao criar a sessão.
        raise PermissionError("Sessão sem apartamento definido.")
    return str(apartamento)


def _validar_data(data: str) -> str | None:
    try:
        return date.fromisoformat(data.strip()).isoformat()
    except (ValueError, AttributeError):
        return None


def _resolver_area(area: str) -> dict | None:
    """Aceita o id ('salao-de-festas') ou o nome ('Salão de festas')."""
    chave = (area or "").strip().lower()
    for a in db.listar_areas():
        if chave in (a["id"], a["nome"].lower()):
            return a
    return None


def _erro_area(area: str) -> dict:
    return {
        "status": "erro",
        "mensagem": f"Área '{area}' não existe.",
        "areas_validas": [a["id"] for a in db.listar_areas()],
    }


_ERRO_DATA = {"status": "erro", "mensagem": "Data inválida. Use o formato AAAA-MM-DD."}


# --------------------------------------------------------------------------
# Reservas
# --------------------------------------------------------------------------


def listar_areas() -> dict:
    """Lista as áreas comuns que podem ser reservadas, com a taxa de cada uma.

    Returns:
        As áreas com id, nome, taxa em reais e se a reserva gera cobrança.
    """
    return {"areas": [{**a, "gera_cobranca": a["taxa"] > 0} for a in db.listar_areas()]}


def verificar_disponibilidade(area: str, data: str) -> dict:
    """Diz se uma área está livre ou ocupada numa data.

    Args:
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data no formato AAAA-MM-DD.

    Returns:
        Apenas se a data está livre ou ocupada. Nunca informa de quem é a reserva.
    """
    a = _resolver_area(area)
    if a is None:
        return _erro_area(area)
    d = _validar_data(data)
    if d is None:
        return _ERRO_DATA
    return {"area": a["id"], "data": d, "disponivel": not db.data_ocupada(a["id"], d)}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta conversa.

    Returns:
        As reservas ativas (código, área e data) do apartamento da sessão.
    """
    return {
        "reservas": db.reservas_do_apartamento(_apartamento_da_sessao(tool_context))
    }


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Áreas com taxa geram cobrança e só são reservadas depois que o morador
    aprova pela confirmação do sistema. Áreas sem taxa são reservadas na hora.

    Args:
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data no formato AAAA-MM-DD.

    Returns:
        O resultado: reservada (com o código), indisponível, aguardando
        confirmação ou recusada pelo morador.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    a = _resolver_area(area)
    if a is None:
        return _erro_area(area)
    d = _validar_data(data)
    if d is None:
        return _ERRO_DATA

    if a["taxa"] > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            # Conferência antecipada só para não pedir confirmação de uma data
            # que já está ocupada. A exclusividade de verdade é garantida na
            # gravação (db.criar_reserva).
            if db.data_ocupada(a["id"], d):
                return {"status": "indisponivel", "area": a["id"], "data": d}
            tool_context.request_confirmation(
                hint=(
                    f"Reservar {a['nome']} em {d} gera cobrança de "
                    f"R$ {a['taxa']:.2f}. Confirma?"
                ),
                payload={"area": a["id"], "data": d, "taxa": a["taxa"]},
            )
            return {
                "status": "aguardando_confirmacao",
                "mensagem": "A reserva gera cobrança e aguarda a confirmação do morador no aplicativo.",
            }
        if not confirmacao.confirmed:
            return {
                "status": "recusada_pelo_morador",
                "mensagem": "O morador não confirmou. Nada foi reservado nem cobrado.",
            }

    resultado = db.criar_reserva(apartamento, a["id"], d)
    if not resultado.criada:
        return {
            "status": "indisponivel",
            "area": a["id"],
            "data": d,
            "mensagem": "A data não está mais disponível. Nada foi reservado nem cobrado.",
        }
    return {
        "status": "reservada",
        "codigo": resultado.codigo,
        "area": a["id"],
        "data": d,
        "cobranca": a["taxa"],
    }


def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
    """Cancela uma reserva do apartamento do morador desta conversa.

    Só encontra reservas do próprio apartamento. Não pede confirmação.

    Args:
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data da reserva no formato AAAA-MM-DD.

    Returns:
        Se a reserva foi cancelada ou se o apartamento não tem essa reserva.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    a = _resolver_area(area)
    if a is None:
        return _erro_area(area)
    d = _validar_data(data)
    if d is None:
        return _ERRO_DATA
    codigo = db.cancelar_reserva(apartamento, a["id"], d)
    if codigo is None:
        return {
            "status": "nao_encontrada",
            "mensagem": "Seu apartamento não tem reserva dessa área nessa data.",
        }
    return {"status": "cancelada", "codigo": codigo, "area": a["id"], "data": d}


# --------------------------------------------------------------------------
# Visitantes
# --------------------------------------------------------------------------


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista os visitantes autorizados do apartamento do morador desta conversa.

    Returns:
        Os visitantes autorizados (nome e data) do apartamento da sessão.
    """
    return {
        "visitantes": db.visitantes_do_apartamento(_apartamento_da_sessao(tool_context))
    }


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio para o apartamento desta conversa.

    Libera acesso, então sempre aguarda a aprovação do morador pela
    confirmação do sistema, mesmo que ele diga na conversa que já confirmou.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.

    Returns:
        O resultado: autorizado, aguardando confirmação ou recusado pelo morador.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    nome_limpo = " ".join((nome or "").split())
    if not nome_limpo:
        return {"status": "erro", "mensagem": "Informe o nome do visitante."}
    d = _validar_data(data)
    if d is None:
        return _ERRO_DATA

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome_limpo} em {d}. Confirma?",
            payload={"nome": nome_limpo, "data": d},
        )
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "A autorização libera acesso ao prédio e aguarda a confirmação do morador no aplicativo.",
        }
    if not confirmacao.confirmed:
        return {
            "status": "recusada_pelo_morador",
            "mensagem": "O morador não confirmou. Nenhuma entrada foi liberada.",
        }
    db.autorizar_visitante(apartamento, nome_limpo, d)
    return {"status": "autorizado", "nome": nome_limpo, "data": d}


# --------------------------------------------------------------------------
# Regulamento
# --------------------------------------------------------------------------


def consultar_capitulo(capitulo: str) -> dict:
    """Devolve o texto de UM capítulo do regulamento interno.

    Args:
        capitulo: número romano do capítulo (ex.: "IV").

    Returns:
        O título e o texto do capítulo pedido.
    """
    c = regulamento.capitulo(capitulo)
    if c is None:
        return {"status": "erro", "mensagem": f"Capítulo '{capitulo}' não existe."}
    return {"capitulo": c.numero, "titulo": c.titulo, "texto": c.texto}
