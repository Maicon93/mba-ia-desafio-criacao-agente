"""Caminhos, nomes e modelos usados pelo projeto."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[2]
load_dotenv(RAIZ / ".env")

# Dados iniciais do condomínio (somente leitura, nunca alterados).
DADOS = RAIZ / "dados"

# Estado mutável: banco do condomínio e banco das sessões do ADK.
VAR = Path(os.getenv("AURORA_VAR_DIR") or RAIZ / "var")
BANCO_CONDOMINIO = VAR / "condominio.db"
BANCO_SESSOES = VAR / "sessoes.db"

APP_NAME = "residencial_aurora"
# O ADK exige um user_id. O "morador" de verdade é o apartamento gravado
# no state da sessão (Garantia 2), então o user_id é fixo.
USER_ID = "morador"

# Chave do state onde fica o apartamento dono da sessão.
STATE_APARTAMENTO = "apartamento"

MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL") or "gemini-3.5-flash"
MODELO_ESPECIALISTAS = os.getenv("MODELO_ESPECIALISTAS") or "gemini-3.5-flash"
