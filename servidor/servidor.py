"""
Servidor central do VAIJUNTO.

Modelo de concorrência: UM THREAD POR CLIENTE.
  * O thread principal fica bloqueado em accept();
  * cada conexão aceita ganha um thread dedicado, que lê requisições em
    laço, executa a operação sobre o Estado compartilhado e responde;
  * as estruturas compartilhadas são protegidas pelas travas do módulo
    `estado` (trava global curta + uma trava por carona).

Por que threads (e não multiplexação com select/asyncio)? O modelo
thread-per-client é o mais simples de raciocinar sobre corretude, e o GIL
do CPython não é gargalo aqui: os threads passam a maior parte do tempo
BLOQUEADOS em I/O de rede (recv), momento em que o GIL é liberado. Para as
dezenas/centenas de clientes do cenário proposto, é mais que suficiente.

Confiabilidade:
  * A queda abrupta de um cliente encerra apenas o seu thread — o accept()
    continua servindo os demais e o estado permanece íntegro, porque toda
    mutação é atômica DENTRO de uma única requisição (não existe reserva
    "pendente" entre mensagens que pudesse ficar órfã).
  * Timeout de inatividade por conexão: um cliente que trava sem fechar o
    socket é desconectado, liberando o thread.
  * Mensagens malformadas são respondidas com erro e descartadas; erros de
    enquadramento (fluxo dessincronizado) fecham só aquela conexão.
"""

import os
import socket
import sys
import threading

from comum import protocolo
from servidor import itinerarios
from servidor.estado import Estado, ErroNegocio, _exigir_texto

# Tempo máximo (segundos) que uma conexão pode ficar sem enviar nada.
TEMPO_LIMITE_INATIVIDADE = 600

# Operações que não exigem sessão autenticada.
OPERACOES_PUBLICAS = {"registrar", "login", "ping"}


class Servidor:
    def __init__(self, host: str, porta: int, caminho_dados: str):
        self.host = host
        self.porta = porta
        self.estado = Estado(caminho_dados)

    # ------------------------------------------------------------------
    # Laço principal
    # ------------------------------------------------------------------
    def iniciar(self) -> None:
        ouvinte = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Permite reiniciar o servidor sem esperar o TIME_WAIT do TCP.
        ouvinte.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ouvinte.bind((self.host, self.porta))
        ouvinte.listen(128)
        print(f"[servidor] VAIJUNTO ouvindo em {self.host}:{self.porta}")
        while True:
            conexao, endereco = ouvinte.accept()
            # daemon=True: threads de cliente não impedem o encerramento
            # do processo do servidor.
            threading.Thread(
                target=self._atender_cliente, args=(conexao, endereco), daemon=True
            ).start()

    # ------------------------------------------------------------------
    # Atendimento de uma conexão
    # ------------------------------------------------------------------
    def _atender_cliente(self, conexao: socket.socket, endereco) -> None:
        rotulo = f"{endereco[0]}:{endereco[1]}"
        print(f"[servidor] conexão aberta: {rotulo}")
        conexao.settimeout(TEMPO_LIMITE_INATIVIDADE)
        # Desativa o algoritmo de Nagle: as mensagens são pequenas e o
        # padrão requisição-resposta se beneficia do envio imediato.
        conexao.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sessao = {"usuario": None}
        try:
            while True:
                try:
                    mensagem = protocolo.receber(conexao)
                except protocolo.MensagemInvalida as exc:
                    # Corpo inválido: descarta, avisa o cliente e segue.
                    protocolo.enviar(conexao, {"status": "erro", "erro": str(exc)})
                    continue
                except protocolo.ErroEnquadramento as exc:
                    # Fluxo dessincronizado: impossível continuar com segurança.
                    print(f"[servidor] {rotulo}: enquadramento inválido ({exc}); encerrando")
                    break
                if mensagem is None:
                    print(f"[servidor] conexão encerrada pelo cliente: {rotulo}")
                    break
                resposta = self._executar(mensagem, sessao)
                protocolo.enviar(conexao, resposta)
        except socket.timeout:
            print(f"[servidor] {rotulo}: inativo por {TEMPO_LIMITE_INATIVIDADE}s; desconectando")
        except (ConnectionError, OSError) as exc:
            # Queda abrupta do cliente (reset, aborto etc.): afeta só ele.
            print(f"[servidor] {rotulo}: conexão perdida ({exc})")
        finally:
            conexao.close()

    # ------------------------------------------------------------------
    # Despacho das operações
    # ------------------------------------------------------------------
    def _executar(self, mensagem: dict, sessao: dict) -> dict:
        op = mensagem.get("op")
        if not isinstance(op, str):
            return {"status": "erro", "erro": "campo 'op' ausente ou inválido"}
        try:
            if op not in OPERACOES_PUBLICAS and sessao["usuario"] is None:
                raise ErroNegocio("operação exige autenticação (faça login primeiro)")
            dados = self._despachar(op, mensagem, sessao)
            return {"status": "ok", "op": op, "dados": dados}
        except ErroNegocio as exc:
            return {"status": "erro", "op": op, "erro": str(exc)}
        except Exception as exc:  # noqa: BLE001 - última linha de defesa:
            # nenhuma requisição pode derrubar o thread com o socket aberto.
            print(f"[servidor] erro interno em '{op}': {exc!r}")
            return {"status": "erro", "op": op, "erro": "erro interno do servidor"}

    def _despachar(self, op: str, msg: dict, sessao: dict):
        estado = self.estado
        usuario = sessao["usuario"]

        if op == "ping":
            return {"pong": True}

        if op == "registrar":
            estado.registrar_usuario(msg.get("usuario"), msg.get("senha"))
            return {"mensagem": "usuário registrado com sucesso"}

        if op == "login":
            sessao["usuario"] = estado.autenticar(msg.get("usuario"), msg.get("senha"))
            return {"usuario": sessao["usuario"]}

        # ----- operações do motorista -----
        if op == "publicar_carona":
            return estado.publicar_carona(
                usuario, msg.get("rota"), msg.get("data"), msg.get("hora"),
                msg.get("assentos"), msg.get("precos"),
            )
        if op == "minhas_caronas":
            return estado.minhas_caronas(usuario)
        if op == "cancelar_carona":
            resultado = estado.cancelar_carona(usuario, msg.get("carona_id"))
            # Problema 2: avisa (melhor esforço) os coordenadores das
            # transações afetadas para desfazerem os itinerários inteiros.
            afetadas = resultado.pop("transacoes_afetadas", [])
            if afetadas:
                self.consorcio.notificar_cancelamentos_de_carona(afetadas)
            return resultado

        # ----- operações do passageiro -----
        if op == "buscar_itinerarios":
            origem = _exigir_texto(msg.get("origem"), "origem", 60)
            destino = _exigir_texto(msg.get("destino"), "destino", 60)
            # Busca DISTRIBUÍDA: catálogo local + consulta aos pares
            # (dispersar-coletar); par caído vira "empresa indisponível".
            return self.consorcio.buscar_distribuida(
                origem, destino, msg.get("data"))
        if op == "reservar":
            # Reserva via 2PC: o servidor desta empresa coordena a transação
            # sobre as empresas donas das caronas (inclusive ele próprio).
            return self.consorcio.reservar_itinerario(usuario, msg.get("etapas"))
        if op == "minhas_reservas":
            return estado.minhas_reservas(usuario)
        if op == "cancelar_reserva":
            return self.consorcio.cancelar_itinerario(usuario, msg.get("reserva_id"))

        raise ErroNegocio(f"operação desconhecida: '{op}'")


def principal() -> None:
    empresa = os.environ.get("EMPRESA", "vaijunto")
    host = os.environ.get("VAIJUNTO_HOST_BIND", "0.0.0.0")
    porta = int(os.environ.get("VAIJUNTO_PORTA", "5000"))
    porta_rest = int(os.environ.get("VAIJUNTO_PORTA_REST", "8000"))
    caminho = os.environ.get(
        "VAIJUNTO_DADOS", os.path.join("dados", f"estado-{empresa}.json"))
    caminho_wal = os.environ.get(
        "VAIJUNTO_WAL", os.path.join("dados", f"wal-{empresa}.jsonl"))

    servidor = Servidor(host, porta, caminho)

    # Camada do consórcio (Problema 2): coordenação 2PC + API REST entre
    # servidores. Sobe em thread própria; a queda de um PAR nunca afeta o
    # atendimento local (timeouts em toda chamada remota).
    from consorcio.nucleo import Consorcio, mapa_de_pares_do_ambiente
    from consorcio.rest_api import criar_api

    servidor.consorcio = Consorcio(
        empresa, servidor.estado, mapa_de_pares_do_ambiente(), caminho_wal)
    api = criar_api(servidor.consorcio)
    threading.Thread(
        target=lambda: api.run(host=host, port=porta_rest, threaded=True,
                               use_reloader=False),
        daemon=True,
    ).start()
    print(f"[servidor] empresa '{empresa}' | TCP {porta} | REST {porta_rest} | "
          f"pares: {sorted(servidor.consorcio.pares.mapa) or 'nenhum'}")

    try:
        servidor.iniciar()
    except KeyboardInterrupt:
        print("\n[servidor] encerrado pelo operador")
        sys.exit(0)


if __name__ == "__main__":
    principal()
