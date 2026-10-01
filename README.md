# Residencial Aurora — assistente virtual

Assistente do aplicativo dos moradores, construído com **Google ADK 2.10.0** e exposto por uma API **FastAPI** em `http://localhost:8000`. O morador reserva áreas comuns, cancela as próprias reservas, autoriza visitantes e tira dúvidas sobre o regulamento.

O modelo decide o caminho da conversa. O que é permitido fica no código: as cinco garantias abaixo valem não importa o que o morador escreva.

```
src/aurora/
├── agents.py         agentes (principal + 3 especialistas) e App do ADK
├── tools.py          tools que leem e gravam os dados do condomínio
├── db.py             armazenamento SQLite (reservas, visitantes, códigos)
├── regulamento.py    leitura do regulamento por capítulo
├── confirmacoes.py   confirmações pendentes a partir dos eventos da sessão
├── api.py            rotas HTTP (contrato do enunciado)
├── config.py         caminhos, nomes e modelos
├── main.py           `uv run api`
└── restaurar.py      `uv run restaurar`
dados/                estado inicial do condomínio (nunca alterado)
var/                  bancos SQLite gerados em tempo de execução (fora do Git)
```

## Arquitetura

```
                   ┌───────────────────────────────┐
  morador ──API──▶ │ assistente (agente principal) │
                   └──┬─────────────┬───────────┬──┘
        transferência │             │           │ AgentTool
                      ▼             ▼           ▼
                 ┌─────────┐  ┌───────────┐  ┌─────────────┐
                 │ reservas│  │ visitantes│  │ regulamento │
                 └────┬────┘  └─────┬─────┘  └──────┬──────┘
                      ▼             ▼               ▼
                 tools de       tools de       consultar_capitulo
                 reserva        visitantes     (um capítulo por vez)
                      └──────┬──────┘
                             ▼
                  SQLite: var/condominio.db
```

Os quatro agentes usam `gemini-3.5-flash`, que pode ser trocado pelas variáveis `MODELO_PRINCIPAL` e `MODELO_ESPECIALISTAS`. As chamadas ao Gemini repetem em caso de 429 ou 5xx (`_modelo` em `agents.py`), para que um pico de uso não vire erro de servidor.

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente` | Conversa com o morador e roteia o pedido. Não tem tools de dados e não recebe o regulamento nas instruções. | Raiz do `App`. Toda mensagem nova começa nele. | Mantém o contexto enxuto e separa "entender o pedido" de "executar". |
| `reservas` | Disponibilidade, reservar, listar e cancelar reservas do apartamento da sessão. | Sub-agente, por **transferência** (`transfer_to_agent`) a partir do `assistente`. | A conversa sobre reserva acontece com o especialista, que tem só as tools do assunto. Ele não transfere para ninguém (`disallow_transfer_to_parent/peers=True`), então nunca vira o agente "ativo" da sessão. Toda mensagem nova do morador volta a começar no `assistente`. |
| `visitantes` | Autorizar entrada e listar visitantes do apartamento da sessão. | Sub-agente, por **transferência**. | Mesmo desenho de `reservas`. |
| `regulamento` | Responder dúvidas lendo só o capítulo pertinente. | **`AgentTool`** do `assistente`. | O `AgentTool` roda o especialista numa sessão separada, em memória, e só a resposta final volta para a sessão do morador. O texto do capítulo nunca entra no histórico (Garantia 4). |

**Retomada da confirmação (o ponto mais delicado).** A resposta do morador precisa chegar ao agente que pediu a confirmação, e quem escolhe esse agente é o Runner. Nos testes com o ADK 2.10.0 e sessão em SQLite, as duas topologias "naturais" falharam:

- **Especialistas que podem transferir de volta:** a retomada funciona, porque o Runner escolhe o último autor "transferível". Mas o especialista fica grudado como agente ativo entre mensagens e precisa decidir sozinho se o assunto é dele. O modelo errava às vezes: recusava, respondia a pedidos antigos ou entrava em pingue-pongue de transferências.
- **Especialistas sem transferência:** o roteamento fica determinístico, sempre pelo `assistente`. Mas a retomada cai no `assistente`, e a tool confirmada nunca roda. É a falha silenciosa descrita no enunciado.
- **Especialistas em `mode="task"`:** falharam com `SessionNotFoundError` dentro do ADK, com sessão em SQLite.

A solução adotada combina o melhor dos dois primeiros casos. Os especialistas não transferem. Para cada um existe um **App de retomada** (`apps_de_retomada` em `agents.py`), com o mesmo nome de App (portanto as mesmas sessões) e o especialista como raiz.

A rota de confirmações lê, nos eventos, o autor do `adk_request_confirmation` (`Pendente.autor` em `confirmacoes.py`) e entrega a resposta pelo Runner desse especialista (`runners_de_retomada` em `api.py`). Assim, a resposta sempre chega ao agente que pediu, sem depender da heurística do Runner. Para pegar regressões, `responder_confirmacao` registra um erro no log se a tool original não for executada depois da resposta (`tool_reexecutada`).

**Armazenamento:** SQLite, sem serviço externo.

- `var/condominio.db` guarda apartamentos, áreas, reservas, visitantes e códigos emitidos.
- `var/sessoes.db` guarda sessões e eventos do ADK, via `SqliteSessionService`.
- Os arquivos de `dados/` são só lidos, na restauração e quando o banco ainda não existe.

## Garantias

### 1. Cobrança ou acesso só com confirmação

- **`src/aurora/tools.py`, `reservar_area`:** se a área tem `taxa > 0` e `tool_context.tool_confirmation` está vazio, chama `tool_context.request_confirmation(payload={"area", "data", "taxa"})` e retorna **sem gravar**. Grava só quando a tool roda de novo com `tool_confirmation.confirmed == True`. Área com taxa zero grava direto, sem confirmação.
- **`src/aurora/tools.py`, `autorizar_visitante`:** o mesmo padrão, sempre, com `payload={"nome", "data"}`.
- **`src/aurora/confirmacoes.py`, `pendentes`:** lê os eventos persistidos da sessão. Uma function call `adk_request_confirmation` sem function response com o mesmo `id` está pendente. É daqui que saem os `confirmacoes_pendentes` de cada resposta.
- **`src/aurora/api.py`, `responder_confirmacao`:** só aceita um `id` que esteja em `pendentes(sessao.events)` desta sessão. Qualquer outro (inexistente, já respondido ou de outra sessão) recebe `409` e nada executa. Para um id válido, envia uma `FunctionResponse(name="adk_request_confirmation", id=..., response={"confirmed": ...})` com o `invocation_id` da invocação pausada, pelo Runner de retomada do especialista que fez o pedido. O ADK retoma e roda a tool original uma vez. A resposta fica gravada na sessão, então reenviar o mesmo `id` dá `409`, inclusive depois de reiniciar.

**Por que não depende do modelo:** o campo `tool_confirmation` só é preenchido pelo ADK quando chega uma function response de `adk_request_confirmation`. Essa function response só é montada pela rota de confirmações; a rota de mensagens envia apenas texto. Escrever "já confirmei" na conversa não tem como preencher esse campo.

### 2. Cada sessão pertence a um apartamento

- **`src/aurora/api.py`, `criar_sessao`:** grava o apartamento uma única vez, em `state={"apartamento": ...}`, na criação da sessão. Nenhuma rota permite alterá-lo.
- **`src/aurora/tools.py`, `_apartamento_da_sessao`:** toda tool que lê ou grava dados de um apartamento obtém o número por aqui, de `tool_context.state`. **Nenhuma tool tem parâmetro de apartamento.**
- **`src/aurora/db.py`:** `reservas_do_apartamento`, `visitantes_do_apartamento`, `cancelar_reserva` e `criar_reserva` filtram ou gravam pelo apartamento recebido da tool.
- **`src/aurora/tools.py`, `verificar_disponibilidade` e `reservar_area`:** usam `db.data_ocupada`, que devolve só `True`/`False`. Nunca devolve o código nem o apartamento da reserva existente.

**Por que não depende do modelo:** o modelo não escolhe o apartamento, porque não existe argumento para isso. Quando o morador diz "sou do 302", o máximo que o modelo consegue é chamar as mesmas tools, que continuam usando o apartamento da sessão. Cancelar a reserva de outro apartamento retorna `nao_encontrada`, e os dados dele nunca chegam ao modelo nem aos eventos.

### 3. Nada se perde no reinício

- **`src/aurora/api.py`:** `session_service = SqliteSessionService(db_path=str(config.BANCO_SESSOES))` persiste sessões, state e eventos em `var/sessoes.db`.
- **`src/aurora/agents.py`, `_RETOMAVEL = ResumabilityConfig(is_resumable=True)`:** usado no `app` principal e nos `apps_de_retomada`. Permite retomar uma invocação pausada numa confirmação, mesmo depois de reiniciar a API, porque o estado da invocação está nos eventos persistidos.
- **`src/aurora/db.py`:** reservas, cancelamentos e visitantes ficam em `var/condominio.db`. Cancelar não apaga a linha, só muda o `status` para `cancelada`.
- **`src/aurora/db.py`, tabela `codigos_emitidos` e `_novo_codigo` / `criar_reserva`:** todo código emitido fica registrado com chave primária e nunca é apagado, nem pela restauração. Um código novo nunca repete outro, inclusive de reserva cancelada.

**Por que não depende do modelo:** é tudo armazenamento em disco. O processo da API não guarda estado em memória.

### 4. O regulamento é consultado, não carregado

- **`src/aurora/agents.py`, `root_agent`:** a instrução do `assistente` não contém o regulamento. Ele só sabe que existe a tool `regulamento`.
- **`src/aurora/agents.py`, `regulamento_agent`:** a instrução traz só o sumário (`regulamento.sumario()`, com números e títulos dos capítulos) e a tool `consultar_capitulo`.
- **`src/aurora/regulamento.py`, `capitulo` / `src/aurora/tools.py`, `consultar_capitulo`:** devolve o texto de um único capítulo.
- **`src/aurora/agents.py`, `AgentTool(agent=regulamento_agent)`:** o especialista roda numa sessão própria em memória (comportamento do `AgentTool`). A sessão do morador recebe só a function call `regulamento` e a resposta final curta. O capítulo lido nunca vira evento da sessão.

**Por que não depende do modelo:** mesmo que o especialista leia mais de um capítulo, isso acontece fora da sessão do morador. O que entra no histórico é só a resposta final.

### 5. Dois moradores, uma reserva

- **`src/aurora/db.py`, `SCHEMA`:** `CREATE UNIQUE INDEX ux_reserva_ativa ON reservas (area, data) WHERE status = 'ativa'`.
- **`src/aurora/db.py`, `criar_reserva`:** grava numa transação `BEGIN IMMEDIATE`. Se outra reserva ativa para a mesma área e data já entrou, o próprio SQLite rejeita o `INSERT` com `IntegrityError`. A transação é desfeita e a função retorna `criada=False`.
- **`src/aurora/tools.py`, `reservar_area`:** transforma `criada=False` em `{"status": "indisponivel"}`, uma resposta normal da tool, e o agente responde que a data não está disponível. Não há erro de servidor: as duas aprovações respondem `200`.

**Por que não depende do modelo:** a conferência em `data_ocupada`, antes de pedir a confirmação, serve só para não pedir confirmação de uma data já ocupada. A exclusividade de verdade é a restrição do banco no instante do `INSERT`. Nenhuma ordem de execução produz duas reservas ativas para a mesma área e data.

## Como rodar

**Pré-requisitos:** [uv](https://docs.astral.sh/uv/), que baixa o Python 3.12 se for preciso, e uma chave do Google AI Studio. Nenhum serviço externo: o armazenamento é SQLite em arquivo.

**Variáveis do `.env`** (copie de `.env.example`):

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio. |
| `MODELO_PRINCIPAL` | não | Modelo do `assistente` (padrão `gemini-3.5-flash`). |
| `MODELO_ESPECIALISTAS` | não | Modelo de `reservas`, `visitantes` e `regulamento` (padrão `gemini-3.5-flash`). |

```bash
cp .env.example .env        # e preencha GOOGLE_API_KEY
uv sync                     # instala as dependências (google-adk==2.10.0)
uv run restaurar            # restaura os dados iniciais
uv run api                  # sobe a API em http://localhost:8000
```

- **`uv run restaurar`:** volta apartamentos, áreas, reservas e visitantes ao conteúdo de `dados/` e **apaga todas as sessões**. Os códigos de reserva já emitidos continuam registrados, para nunca se repetirem. Rode com a API parada.
- **`uv run api`:** sobe a API. Se o banco ainda não existir, ele é criado com os dados iniciais. Pare com `Ctrl+C` e suba de novo com o mesmo comando: conversas e dados continuam lá.

### Rotas

| Método | Caminho | Descrição |
|---|---|---|
| `POST` | `/sessoes` | `{"apartamento": "101"}` → `201 {"session_id"}` |
| `POST` | `/sessoes/{session_id}/mensagens` | `{"texto"}` → `{"resposta", "confirmacoes_pendentes"}` |
| `POST` | `/sessoes/{session_id}/confirmacoes` | `{"id", "confirmado"}` → mesmo formato; `409` se o id não está pendente nesta sessão |
| `GET` | `/sessoes/{session_id}/eventos` | todos os eventos da sessão, em ordem e completos |
| `GET` | `/apartamentos/{numero}/reservas` | reservas ativas (verificação, sem modelo) |
| `GET` | `/apartamentos/{numero}/visitantes` | visitantes autorizados (verificação, sem modelo) |

As rotas com `{session_id}` respondem `404` quando a sessão não existe. Criar sessão para um apartamento que não está em `dados/apartamentos.json` também responde `404`.
