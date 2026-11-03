"""
Cliente HTTP para falar com os servidores das outras empresas (os "pares").

Toda chamada tem TIMEOUT curto: a queda de um par nunca pode travar este
servidor — ela é detectada pelo estouro do tempo e tratada como resposta
negativa/indisponível, nunca como erro fatal (restrição do problema).

O relógio de Lamport viaja no cabeçalho X-Lamport das requisições e das
respostas, mantendo a ordem causal entre os servidores.
"""

import requests

# (conexão, leitura) em segundos: detecta par caído em ~1 s.
TIMEOUT = (1.0, 4.0)


class Pares:
    def __init__(self, empresa: str, mapa: dict[str, str], relogio):
        """`mapa`: nome da empresa -> URL base da API REST dela."""
        self.empresa = empresa
        self.mapa = dict(mapa)
        self.relogio = relogio
        self._sessao = requests.Session()

    def url_de(self, empresa: str) -> str | None:
        return self.mapa.get(empresa)

    def _chamar(self, metodo: str, empresa: str, rota: str, corpo=None):
        """Faz a chamada e devolve (status_code, json) — ou (None, None) se o
        par está inalcançável (timeout, conexão recusada etc.)."""
        base = self.mapa.get(empresa)
        if base is None:
            return None, None
        try:
            resposta = self._sessao.request(
                metodo, base.rstrip("/") + rota, json=corpo, timeout=TIMEOUT,
                headers={"X-Lamport": str(self.relogio.tique()),
                         "X-Empresa": self.empresa})
        except requests.RequestException:
            return None, None
        self.relogio.observar(resposta.headers.get("X-Lamport"))
        try:
            dados = resposta.json()
        except ValueError:
            dados = None
        return resposta.status_code, dados

    # ----- operações usadas pelo consórcio -----
    def listar_caronas(self, empresa: str, data: str):
        return self._chamar("GET", empresa, f"/caronas?data={data}")

    def preparar(self, empresa: str, tx: str, corpo: dict):
        return self._chamar("POST", empresa, f"/transacoes/{tx}/prepare", corpo)

    def efetivar(self, empresa: str, tx: str):
        return self._chamar("POST", empresa, f"/transacoes/{tx}/commit")

    def abortar(self, empresa: str, tx: str):
        return self._chamar("POST", empresa, f"/transacoes/{tx}/abort")

    def status_transacao(self, empresa: str, tx: str):
        return self._chamar("GET", empresa, f"/transacoes/{tx}")

    def cancelar_reserva(self, empresa: str, tx: str):
        return self._chamar("DELETE", empresa, f"/reservas/{tx}")

    def notificar_cancelamento(self, empresa: str, tx: str, motivo: str):
        return self._chamar("POST", empresa, "/notificacoes/cancelamento",
                            {"tx": tx, "motivo": motivo})
