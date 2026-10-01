"""API HTTP do assistente (contrato do enunciado)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from google.adk.runners import Runner
from google.adk.sessions import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel

from . import config, db
from .agents import app as adk_app
from .agents import apps_de_retomada
from .confirmacoes import PEDIDO_CONFIRMACAO, como_json, pendentes, tool_reexecutada

logger = logging.getLogger("aurora")

# Garantia 3: sessões e eventos persistidos em SQLite, então reiniciar a API
# não perde nenhuma conversa.
config.VAR.mkdir(parents=True, exist_ok=True)
session_service = SqliteSessionService(db_path=str(config.BANCO_SESSOES))
runner = Runner(app=adk_app, session_service=session_service)
# Runners usados só para entregar a resposta de uma confirmação ao especialista
# que a pediu (mesmo nome de App, mesmas sessões).
runners_de_retomada = {
    nome: Runner(app=app_especialista, session_service=session_service)
    for nome, app_especialista in apps_de_retomada.items()
}

# Serializa as requisições de uma mesma sessão (uma conversa não processa duas
# mensagens ao mesmo tempo). Sessões diferentes continuam em paralelo.
_travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.garantir_banco()
    yield


api = FastAPI(title="Residencial Aurora", lifespan=lifespan)


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


async def _sessao(session_id: str) -> Session:
    sessao = await session_service.get_session(
        app_name=config.APP_NAME, user_id=config.USER_ID, session_id=session_id
    )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return sessao


def _texto(evento) -> str:
    if evento.author == "user" or evento.partial or not evento.content:
        return ""
    return "".join(
        p.text for p in (evento.content.parts or []) if p.text and not p.thought
    ).strip()


async def _executar(
    session_id: str,
    mensagem: types.Content,
    invocation_id: str | None = None,
    executor: Runner = runner,
) -> dict[str, Any]:
    textos: list[str] = []
    async for evento in executor.run_async(
        user_id=config.USER_ID,
        session_id=session_id,
        invocation_id=invocation_id,
        new_message=mensagem,
    ):
        if t := _texto(evento):
            textos.append(t)
    sessao = await _sessao(session_id)
    return {
        "resposta": "\n\n".join(textos),
        "confirmacoes_pendentes": como_json(pendentes(sessao.events)),
    }


@api.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao) -> dict[str, str]:
    apartamento = corpo.apartamento.strip()
    if not db.apartamento_existe(apartamento):
        raise HTTPException(status_code=404, detail="Apartamento não encontrado.")
    # Garantia 2: o apartamento é gravado UMA vez no state, aqui. As tools
    # leem daqui; nenhuma rota ou tool permite trocá-lo depois.
    sessao = await session_service.create_session(
        app_name=config.APP_NAME,
        user_id=config.USER_ID,
        state={config.STATE_APARTAMENTO: apartamento},
    )
    return {"session_id": sessao.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: NovaMensagem) -> dict[str, Any]:
    await _sessao(session_id)
    async with _travas[session_id]:
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(session_id, mensagem)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(
    session_id: str, corpo: RespostaConfirmacao
) -> dict[str, Any]:
    await _sessao(session_id)
    async with _travas[session_id]:
        # Garantia 1: só aceita um id que esteja pendente NESTA sessão. Um id já
        # respondido, inexistente ou de outra sessão recebe 409 e nada executa.
        sessao = await _sessao(session_id)
        pendente = next((p for p in pendentes(sessao.events) if p.id == corpo.id), None)
        if pendente is None:
            raise HTTPException(
                status_code=409,
                detail="Não existe confirmação pendente com esse id nesta sessão.",
            )
        # Devolve a resposta do morador ao ADK como function response do
        # `adk_request_confirmation`, pelo Runner do agente que fez o pedido.
        # Ele retoma a invocação pausada e roda a tool original de novo, agora
        # com `tool_context.tool_confirmation` preenchido.
        executor = runners_de_retomada.get(pendente.autor, runner)
        resposta = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=pendente.id,
                        name=PEDIDO_CONFIRMACAO,
                        response={"confirmed": corpo.confirmado},
                    )
                )
            ],
        )
        resultado = await _executar(
            session_id,
            resposta,
            invocation_id=pendente.invocation_id,
            executor=executor,
        )
        if not tool_reexecutada((await _sessao(session_id)).events, pendente):
            logger.error(
                "Confirmação %s respondida, mas a tool %s não foi retomada.",
                pendente.id,
                pendente.acao,
            )
        return resultado


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list[dict[str, Any]]:
    sessao = await _sessao(session_id)
    return [
        e.model_dump(mode="json", by_alias=True, exclude_none=True)
        for e in sessao.events
    ]


# --------------------------------------------------------------------------
# Rotas de verificação: leem o banco direto, sem passar pelo modelo.
# --------------------------------------------------------------------------


@api.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str) -> list[dict[str, str]]:
    return db.reservas_do_apartamento(numero)


@api.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str) -> list[dict[str, str]]:
    return db.visitantes_do_apartamento(numero)
