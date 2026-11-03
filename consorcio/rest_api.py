"""
API REST do consórcio — a interface que cada servidor de empresa oferece
AOS DEMAIS SERVIDORES (a comunicação com os clientes motorista/passageiro
continua no protocolo TCP do Problema 1).

REST foi exigido pelo problema para atravessar firewalls corporativos:
tráfego HTTP em porta conhecida, com recursos e métodos padronizados.
O framework (Flask) é permitido; a COORDENAÇÃO (2PC, exclusão mútua,
recuperação) é toda nossa — o framework só roteia requisições.

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

from flask import Flask, g, jsonify, request

from servidor.estado import ErroNegocio


def criar_api(consorcio) -> Flask:
    app = Flask(f"consorcio-{consorcio.empresa}")

    @app.before_request
    def _receber_lamport():
        consorcio.relogio.observar(request.headers.get("X-Lamport"))
        g.empresa_remota = request.headers.get("X-Empresa", "?")

    @app.after_request
    def _devolver_lamport(resposta):
        resposta.headers["X-Lamport"] = str(consorcio.relogio.tique())
        resposta.headers["X-Empresa"] = consorcio.empresa
        return resposta

    @app.errorhandler(Exception)
    def _erro_interno(exc):
        # Nenhuma requisição derruba o servidor; erro vira resposta 500.
        if isinstance(exc, ErroNegocio):
            return jsonify({"erro": str(exc)}), 400
        print(f"[rest] erro interno: {exc!r}")
        return jsonify({"erro": "erro interno"}), 500

    @app.get("/ping")
    def ping():
        return jsonify({"empresa": consorcio.empresa,
                        "lamport": consorcio.relogio.valor})

    @app.get("/caronas")
    def caronas():
        data = request.args.get("data", "")
        try:
            lista = consorcio.estado.snapshot_caronas(data)
        except ErroNegocio as exc:
            return jsonify({"erro": str(exc)}), 400
        return jsonify({"empresa": consorcio.empresa, "caronas": lista})

    # ----- papel de PARTICIPANTE do 2PC -----
    @app.post("/transacoes/<tx>/prepare")
    def prepare(tx):
        corpo = request.get_json(silent=True) or {}
        try:
            voto = consorcio.estado.preparar_hold(
                tx, corpo.get("etapas"), corpo.get("chave"),
                corpo.get("coordenador", g.empresa_remota),
                corpo.get("passageiro", "?"))
        except ErroNegocio as exc:
            voto = {"voto": "nao", "motivo": str(exc)}
        codigo = {"sim": 200, "aguarde": 423, "nao": 409}[voto["voto"]]
        return jsonify(voto), codigo

    @app.post("/transacoes/<tx>/commit")
    def commit(tx):
        reserva = consorcio.estado.efetivar_hold(tx)
        if reserva is None:
            # Hold desconhecido ou já expirado: tarde demais para efetivar.
            return jsonify({"erro": "transação desconhecida ou expirada"}), 410
        return jsonify({"resultado": "efetivada", "reserva_id": reserva["id"]})

    @app.post("/transacoes/<tx>/abort")
    def abort(tx):
        consorcio.estado.abortar_hold(tx)
        return jsonify({"resultado": "abortada"})

    # ----- papel de COORDENADOR (consultado pelos participantes) -----
    @app.get("/transacoes/<tx>")
    def status(tx):
        return jsonify({"tx": tx, "estado": consorcio.status_transacao(tx)})

    @app.delete("/reservas/<tx>")
    def cancelar_reserva(tx):
        return jsonify(consorcio.estado.cancelar_reserva_tx(tx))

    @app.post("/notificacoes/cancelamento")
    def notificacao_cancelamento():
        corpo = request.get_json(silent=True) or {}
        tx = corpo.get("tx")
        if not isinstance(tx, str):
            return jsonify({"erro": "campo 'tx' ausente"}), 400
        consorcio.tratar_notificacao_cancelamento(
            tx, corpo.get("motivo", "cancelada_pelo_motorista"))
        return jsonify({"resultado": "ok"})

    return app
