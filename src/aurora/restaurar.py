"""Restaura o estado inicial do condomínio (`uv run restaurar`).

Volta apartamentos, áreas, reservas e visitantes ao conteúdo de `dados/` e
apaga todas as sessões. Os códigos de reserva já emitidos continuam
registrados, para que nenhum código novo repita um antigo.
Rode com a API parada.
"""

from . import config, db


def main() -> None:
    db.restaurar_dados()
    for sufixo in ("", "-wal", "-shm"):
        caminho = config.BANCO_SESSOES.with_name(config.BANCO_SESSOES.name + sufixo)
        caminho.unlink(missing_ok=True)
    print("Dados restaurados a partir de dados/ e sessões apagadas.")


if __name__ == "__main__":
    main()
