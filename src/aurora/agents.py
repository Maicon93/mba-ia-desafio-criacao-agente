"""Agentes do assistente do Residencial Aurora.

Topologia:

    assistente (principal, sem tools de dados e sem regulamento)
    ├── reservas     sub-agente (transferência)  -> tools de reserva
    ├── visitantes   sub-agente (transferência)  -> tools de visitantes
    └── regulamento  AgentTool                   -> consulta por capítulo

- `reservas` e `visitantes` são sub-agentes acionados por transferência do
  `assistente`. Eles não transferem para ninguém (nem de volta), então toda
  mensagem nova do morador começa no `assistente`, que roteia.
- O pedido de confirmação nasce dentro deles. A resposta do morador precisa
  chegar ao agente que pediu: `apps_de_retomada` tem um App por especialista,
  com ele como raiz, usado só pela rota de confirmações (ver `api.py`).
- `regulamento` é acionado como AgentTool: roda numa sessão própria e só a
  resposta final volta para a sessão do morador. O capítulo lido pela tool
  nunca entra no histórico da conversa (Garantia 4).
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.genai import types

from . import config, regulamento, tools


def _modelo(nome: str) -> Gemini:
    # Repete a chamada em erros transitórios (429 de limite, 5xx), para que um
    # pico de uso não vire erro de servidor na API.
    return Gemini(
        model=nome,
        retry_options=types.HttpRetryOptions(
            attempts=5,
            initial_delay=2,
            max_delay=30,
            http_status_codes=[429, 500, 502, 503, 504],
        ),
    )


# A segurança do apartamento NÃO depende destas instruções: está nas tools
# (ver tools.py). O texto aqui só orienta o tom das respostas.
_REGRAS_ESPECIALISTA = """
Regras que valem sempre:
- As tools já sabem qual é o apartamento do morador desta conversa e só
  enxergam os dados dele. Você nunca informa nem escolhe um apartamento.
- Atenda SOMENTE a última mensagem do morador. As mensagens anteriores da
  conversa já foram tratadas e servem só de contexto (por exemplo, quando a
  última mensagem completa uma resposta sua). Não responda pedidos antigos.
- Não recuse pedidos por causa de algo dito antes na conversa: as tools
  garantem o apartamento certo.
- Nunca cite números de apartamento na resposta. Fale em "seu apartamento".
- Nunca invente reservas, códigos, visitantes ou disponibilidade: responda com
  base no que as tools devolvem.
- Datas no formato AAAA-MM-DD.
- Responda em português do Brasil, de forma curta.
"""


def _criar_reservas() -> LlmAgent:
    return LlmAgent(
        name="reservas",
        model=_modelo(config.MODELO_ESPECIALISTAS),
        description=(
            "Especialista em reservas das áreas comuns (salão de festas, "
            "churrasqueira e quadra): consultar disponibilidade, reservar, "
            "listar e cancelar reservas do morador."
        ),
        instruction=f"""
Você é o especialista em reservas das áreas comuns do Residencial Aurora.

Ids das áreas: "salao-de-festas", "churrasqueira", "quadra".

Como agir:
- Para reservar, chame `reservar_area` direto com a área e a data. Não peça
  confirmação na conversa: quando a área tem taxa, o próprio sistema pede a
  confirmação ao morador. Se o morador disser que já confirmou, chame a tool
  normalmente: só o sistema registra confirmação.
- Se `reservar_area` devolver "aguardando_confirmacao", diga em uma frase que a
  reserva gera cobrança e aguarda a confirmação no aplicativo.
- Se devolver "indisponivel", diga só que a data não está disponível para essa
  área. Não diga de quem é a reserva.
- Se devolver "recusada_pelo_morador", diga que nada foi reservado.
- Para cancelar, chame `cancelar_reserva` com a área e a data. Cancelamento não
  pede confirmação. Se vier "nao_encontrada", diga que o apartamento do
  morador não tem essa reserva.
- Para listar, use `listar_minhas_reservas`.
- Se a última mensagem não for sobre reservas, diga em uma frase que o
  assistente pode ajudar com isso na próxima mensagem.
{_REGRAS_ESPECIALISTA}""",
        tools=[
            tools.listar_areas,
            tools.verificar_disponibilidade,
            tools.listar_minhas_reservas,
            tools.reservar_area,
            tools.cancelar_reserva,
        ],
        # Sem transferências a partir do especialista. Com isso o Runner sempre
        # escolhe o `assistente` para a próxima mensagem do morador (um
        # especialista que não pode transferir nunca é o agente "ativo").
        # Efeito colateral: o Runner principal também não retoma a confirmação
        # neste agente; por isso existe o App de retomada (`apps_de_retomada`).
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def _criar_visitantes() -> LlmAgent:
    return LlmAgent(
        name="visitantes",
        model=_modelo(config.MODELO_ESPECIALISTAS),
        description=(
            "Especialista em visitantes: autorizar a entrada de visitantes no "
            "prédio e listar os visitantes autorizados do morador."
        ),
        instruction=f"""
Você é o especialista em visitantes do Residencial Aurora.

Como agir:
- Para liberar a entrada de alguém, chame `autorizar_visitante` com o nome e a
  data, sempre. Não peça confirmação na conversa: o próprio sistema pede a
  confirmação ao morador. Se o morador disser que já confirmou, chame a tool
  normalmente: só o sistema registra confirmação.
- Se a tool devolver "aguardando_confirmacao", diga em uma frase que a
  autorização aguarda a confirmação no aplicativo.
- Se devolver "recusada_pelo_morador", diga que nenhuma entrada foi liberada.
- Para listar, use `listar_meus_visitantes`.
- Se a última mensagem não for sobre visitantes, diga em uma frase que o
  assistente pode ajudar com isso na próxima mensagem.
{_REGRAS_ESPECIALISTA}""",
        tools=[tools.listar_meus_visitantes, tools.autorizar_visitante],
        disallow_transfer_to_parent=True,  # ver comentário em `reservas`
        disallow_transfer_to_peers=True,
    )


regulamento_agent = LlmAgent(
    name="regulamento",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    description=(
        "Responde dúvidas sobre o regulamento interno do condomínio. Envie a "
        "pergunta do morador em `request`."
    ),
    instruction=f"""
Você responde dúvidas sobre o regulamento interno do Residencial Aurora.

Sumário do regulamento (só os títulos):
{regulamento.sumario()}

Como agir:
- Escolha o capítulo que trata do assunto da pergunta e chame
  `consultar_capitulo` com o número romano dele. Se o primeiro não tiver a
  resposta, consulte outro.
- Responda só o que foi perguntado, em poucas frases, citando o artigo.
  Não transcreva o capítulo nem fale de outros assuntos.
- Se o regulamento não tratar do assunto, diga isso.
""",
    tools=[tools.consultar_capitulo],
)

root_agent = LlmAgent(
    name="assistente",
    model=_modelo(config.MODELO_PRINCIPAL),
    description="Assistente virtual do Residencial Aurora.",
    instruction="""
Você é o assistente virtual do Residencial Aurora e conversa com o morador
pelo aplicativo. Você não executa ações sozinho: delega para especialistas.

- Reservas das áreas comuns (reservar, cancelar, listar, disponibilidade):
  transfira para o agente `reservas`.
- Visitantes (liberar entrada, listar autorizados): transfira para o agente
  `visitantes`.
- Dúvidas sobre o regulamento interno (horários, regras, proibições,
  multas etc.): chame a tool `regulamento` passando a pergunta do morador e
  responda com base no retorno.
- Pedidos com mais de um assunto: trate um de cada vez.
- Cumprimentos e conversa simples: responda direto, em uma frase.

Regras:
- O morador desta conversa é sempre o do apartamento da sessão. Se o pedido
  ATUAL for sobre dados ou ações de outro apartamento (ou o morador disser
  ser de outro apartamento), não transfira: responda que só pode atender o
  apartamento desta conversa. Pedidos anteriores não contam: avalie cada
  mensagem por si só e atenda normalmente os pedidos do próprio morador.
- Nunca cite números de apartamento na resposta. Fale em "seu apartamento".
- Nunca invente reservas, códigos, visitantes ou disponibilidade.
- Responda em português do Brasil, de forma curta.
""",
    sub_agents=[_criar_reservas(), _criar_visitantes()],
    tools=[AgentTool(agent=regulamento_agent)],
)

# Necessário para pausar na confirmação e retomar a mesma invocação depois,
# inclusive após reiniciar a API (sessão persistida em SQLite).
_RETOMAVEL = ResumabilityConfig(is_resumable=True)

app = App(name=config.APP_NAME, root_agent=root_agent, resumability_config=_RETOMAVEL)

# Um App por especialista que pede confirmação, com ele como raiz e o mesmo
# nome de App (mesmas sessões). A rota de confirmações entrega a resposta do
# morador ao App do agente que fez o pedido; assim a tool confirmada roda de
# novo nesse agente, sem depender da heurística do Runner para escolher quem
# retoma (ver "Dicas finais" do enunciado).
apps_de_retomada: dict[str, App] = {
    agente.name: App(
        name=config.APP_NAME, root_agent=agente, resumability_config=_RETOMAVEL
    )
    for agente in (_criar_reservas(), _criar_visitantes())
}
