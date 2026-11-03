"""
API REST do consórcio — a interface que cada servidor de empresa oferece
AOS DEMAIS SERVIDORES (a comunicação com os clientes motorista/passageiro
continua no protocolo TCP do Problema 1).

REST foi exigido pelo problema para atravessar firewalls corporativos:
tráfego HTTP em porta conhecida, com recursos e métodos padronizados.
O framework (FastAPI) é permitido; a COORDENAÇÃO (2PC, exclusão mútua,
recuperação) é toda nossa — o framework só roteia requisições.

Os handlers são funções SÍNCRONAS de propósito: o FastAPI as executa em
um threadpool, preservando o modelo thread-por-requisição do restante do
servidor (as travas do Estado e o fsync do WAL podem bloquear à vontade
sem travar o loop de eventos).

Recursos:
  GET    /ping                        saúde do servidor
  GET    /caronas?data=AAAA-MM-DD     catálogo local (busca dispersar-coletar)
  POST   /transacoes/<tx>/prepare     2PC fase 1 (voto + hold com lease)
  POST   /transacoes/<tx>/commit      2PC fase 2 (efetiva; idempotente)
  POST   /transacoes/<tx>/abort       2PC fase 2 (aborta; idempotente)
  GET    /transacoes/<tx>             status no coordenador (pull de recuperação)
  DELETE /reservas/<tx>               cancela a reserva da transação (pós-commit)
  POST   /notificacoes/cancelamento   carona cancelada -> coordenador desfaz tudo

O relógio de Lamport viaja nos cabeçalhos X-Lamport (requisição e resposta).
"""

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from servidor.estado import ErroNegocio


class _MiddlewareLamport:
    """Middleware ASGI puro para o relógio de Lamport.

    Observa o relógio do remetente na entrada e carimba o próprio na
    saída — todo par de mensagens avança a ordem causal. É ASGI cru (e
    não o `@app.middleware("http")` do Starlette) porque o BaseHTTPMiddleware
    embrulha cada requisição em tarefas extras e custa caro sob carga.
    """

    def __init__(self, app, consorcio):
        self.app = app
        self.consorcio = consorcio
        self._extras = [(b"x-empresa", consorcio.empresa.encode())]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        recebido = next((v for k, v in scope["headers"]
                         if k == b"x-lamport"), None)
        self.consorcio.relogio.observar(
            recebido.decode("ascii", "replace") if recebido else None)

        async def enviar(mensagem):
            if mensagem["type"] == "http.response.start":
                mensagem["headers"] = list(mensagem.get("headers") or []) + [
                    (b"x-lamport",
                     str(self.consorcio.relogio.tique()).encode()),
                ] + self._extras
            await send(mensagem)

        await self.app(scope, receive, enviar)


def criar_api(consorcio) -> FastAPI:
    app = FastAPI(title=f"consorcio-{consorcio.empresa}")
    app.add_middleware(_MiddlewareLamport, consorcio=consorcio)

    @app.exception_handler(ErroNegocio)
    def _erro_negocio(request: Request, exc: ErroNegocio):
        return JSONResponse({"erro": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    def _erro_interno(request: Request, exc: Exception):
        # Nenhuma requisição derruba o servidor; erro vira resposta 500.
        print(f"[rest] erro interno: {exc!r}")
        return JSONResponse({"erro": "erro interno"}, status_code=500)

    @app.get("/ping")
    def ping():
        return {"empresa": consorcio.empresa,
                "lamport": consorcio.relogio.valor}

    @app.get("/caronas")
    def caronas(data: str = ""):
        try:
            lista = consorcio.estado.snapshot_caronas(data)
        except ErroNegocio as exc:
            return JSONResponse({"erro": str(exc)}, status_code=400)
        return {"empresa": consorcio.empresa, "caronas": lista}

    # ----- papel de PARTICIPANTE do 2PC -----
    @app.post("/transacoes/{tx}/prepare")
    def prepare(tx: str, request: Request, corpo: dict = Body(default=None)):
        corpo = corpo or {}
        try:
            voto = consorcio.estado.preparar_hold(
                tx, corpo.get("etapas"), corpo.get("chave"),
                corpo.get("coordenador",
                          request.headers.get("X-Empresa", "?")),
                corpo.get("passageiro", "?"))
        except ErroNegocio as exc:
            voto = {"voto": "nao", "motivo": str(exc)}
        codigo = {"sim": 200, "aguarde": 423, "nao": 409}[voto["voto"]]
        return JSONResponse(voto, status_code=codigo)

    @app.post("/transacoes/{tx}/commit")
    def commit(tx: str):
        reserva = consorcio.estado.efetivar_hold(tx)
        if reserva is None:
            # Hold desconhecido ou já expirado: tarde demais para efetivar.
            return JSONResponse(
                {"erro": "transação desconhecida ou expirada"}, status_code=410)
        return {"resultado": "efetivada", "reserva_id": reserva["id"]}

    @app.post("/transacoes/{tx}/abort")
    def abort(tx: str):
        consorcio.estado.abortar_hold(tx)
        return {"resultado": "abortada"}

    # ----- papel de COORDENADOR (consultado pelos participantes) -----
    @app.get("/transacoes/{tx}")
    def status(tx: str):
        return {"tx": tx, "estado": consorcio.status_transacao(tx)}

    @app.delete("/reservas/{tx}")
    def cancelar_reserva(tx: str):
        return consorcio.estado.cancelar_reserva_tx(tx)

    @app.post("/notificacoes/cancelamento")
    def notificacao_cancelamento(corpo: dict = Body(default=None)):
        corpo = corpo or {}
        tx = corpo.get("tx")
        if not isinstance(tx, str):
            return JSONResponse({"erro": "campo 'tx' ausente"}, status_code=400)
        consorcio.tratar_notificacao_cancelamento(
            tx, corpo.get("motivo", "cancelada_pelo_motorista"))
        return {"resultado": "ok"}

    return app
