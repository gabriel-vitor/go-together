"""
Núcleo do consórcio: coordenação distribuída entre as empresas.

Cada servidor de empresa carrega UMA instância de `Consorcio`, que acumula
dois papéis (não há coordenador fixo — restrição do problema):

* COORDENADOR das transações iniciadas pelos SEUS passageiros: executa o
  Two-Phase Commit (2PC) sobre os participantes (as empresas donas das
  caronas do itinerário, incluindo ele mesmo), decide o resultado e o grava
  no WAL ANTES de comunicá-lo;
* PARTICIPANTE das transações das outras empresas: vota no PREPARE criando
  holds com lease (ver estado.py) e aplica COMMIT/ABORT idempotentes.

Prevenção de deadlock distribuído (duas camadas):
  1. os PREPAREs são enviados em ORDEM CANÔNICA (empresas em ordem
     alfabética) — a mesma ordenação total de recursos do Problema 1,
     agora entre servidores;
  2. wait-die com a chave de Lamport (ver estado.preparar_hold): em caso de
     disputa, a transação mais antiga aguarda e a mais nova morre — nunca
     há espera circular.

Tolerância a falhas:
  * participante caiu durante o PREPARE -> timeout vira voto "não" e o
    coordenador aborta os demais;
  * coordenador caiu antes de decidir -> os leases dos holds expiram e os
    participantes liberam os assentos (presunção de aborto);
  * coordenador caiu depois de decidir -> a decisão está no WAL; na
    recuperação os COMMITs/ABORTs pendentes são reenviados (idempotentes);
  * janela rara (participante presumiu aborto após o commit de outros) ->
    COMPENSAÇÃO: o coordenador cancela a transação inteira em todos, e o
    consórcio volta a um estado consistente.
"""

import os
import threading
import time
import uuid

from consorcio.lamport import RelogioLamport
from consorcio.pares import Pares
from consorcio.wal import WAL
from servidor import itinerarios
from servidor.estado import ErroNegocio, LEASE_SEGUNDOS

# Tentativas de PREPARE quando o participante responde "aguarde" (wait-die).
TENTATIVAS_AGUARDE = 8
INTERVALO_AGUARDE = 0.5
# Carência máxima para um hold cujo coordenador está inalcançável: depois
# disso o participante presume aborto e libera os assentos.
CARENCIA_PRESUNCAO = LEASE_SEGUNDOS * 3


class Consorcio:
    def __init__(self, empresa: str, estado, mapa_pares: dict[str, str],
                 caminho_wal: str):
        self.empresa = empresa
        self.estado = estado
        self.relogio = RelogioLamport()
        self.wal = WAL(caminho_wal)
        self.pares = Pares(empresa, mapa_pares, self.relogio)
        # Registro do COORDENADOR: tx -> {estado, participantes, pendentes...}
        # Reconstruído inteiramente do WAL na inicialização.
        self.transacoes: dict[str, dict] = {}
        self.trava = threading.RLock()
        self.pendencias_cancel: list[tuple[str, str]] = []  # (empresa, tx)
        self._primeiro_vencimento: dict[str, float] = {}  # tx -> epoch
        self._recuperar()
        threading.Thread(target=self._varredor, daemon=True).start()

    # ------------------------------------------------------------------
    # Identificadores globais ("empresa:carona")
    # ------------------------------------------------------------------
    def separar_id(self, carona_id: str) -> tuple[str, str]:
        """'expresso:car-abc' -> ('expresso', 'car-abc'); sem prefixo é local."""
        if isinstance(carona_id, str) and ":" in carona_id:
            empresa, local = carona_id.split(":", 1)
            return empresa.strip(), local.strip()
        return self.empresa, carona_id

    def _globalizar_carona(self, carona: dict, empresa: str) -> dict:
        c = dict(carona)
        c["id"] = f"{empresa}:{c['id']}"
        c["empresa"] = empresa
        c["motorista"] = f"{c['motorista']}@{empresa}"
        return c

    # ------------------------------------------------------------------
    # Busca distribuída (dispersar-coletar)
    # ------------------------------------------------------------------
    def buscar_distribuida(self, origem: str, destino: str, data: str) -> dict:
        """Consulta o catálogo local e, EM PARALELO, o de cada par (com
        timeout). Par fora do ar não trava a busca: ele é apenas marcado
        como indisponível e a resposta sai parcial — a estratégia é a
        consulta em tempo real (dispersar-coletar), que dispensa réplicas
        e nunca mostra carona de empresa caída (que não poderia confirmar)."""
        caronas = [self._globalizar_carona(c, self.empresa)
                   for c in self.estado.snapshot_caronas(data)]
        indisponiveis: list[str] = []
        resultados: dict[str, list | None] = {}

        def consultar(nome_par: str):
            status, dados = self.pares.listar_caronas(nome_par, data)
            if status == 200 and isinstance(dados, dict):
                resultados[nome_par] = dados.get("caronas", [])
            else:
                resultados[nome_par] = None

        threads = [threading.Thread(target=consultar, args=(p,))
                   for p in self.pares.mapa]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for nome_par, lista in resultados.items():
            if lista is None:
                indisponiveis.append(nome_par)
            else:
                caronas.extend(self._globalizar_carona(c, nome_par) for c in lista)

        return {
            "itinerarios": itinerarios.buscar(caronas, origem, destino),
            "empresas_indisponiveis": sorted(indisponiveis),
        }

    # ------------------------------------------------------------------
    # COORDENADOR: reserva distribuída (2PC)
    # ------------------------------------------------------------------
    def reservar_itinerario(self, usuario: str, etapas) -> dict:
        if not isinstance(etapas, list) or not (1 <= len(etapas) <= 4):
            raise ErroNegocio("etapas deve ser uma lista de 1 a 4 itens")
        for p in etapas:
            if not isinstance(p, dict) or not isinstance(p.get("carona_id"), str):
                raise ErroNegocio("cada etapa deve ser um objeto com carona_id")

        # Contiguidade do itinerário completo (checada aqui porque cada
        # participante só enxerga as etapas das SUAS caronas).
        for ant, seg in zip(etapas, etapas[1:]):
            if str(ant.get("destino", "")).casefold() != str(seg.get("origem", "")).casefold():
                raise ErroNegocio(
                    "itinerário descontínuo: o destino de uma etapa deve ser a origem da próxima")

        # Agrupa as etapas por empresa dona, preservando a posição original
        # para remontar o itinerário resolvido na ordem da viagem.
        por_empresa: dict[str, list[tuple[int, dict]]] = {}
        for posicao, p in enumerate(etapas):
            empresa, local = self.separar_id(p["carona_id"])
            if empresa != self.empresa and empresa not in self.pares.mapa:
                raise ErroNegocio(f"empresa desconhecida no consórcio: '{empresa}'")
            etapa_local = {"carona_id": local,
                           "origem": p.get("origem"), "destino": p.get("destino")}
            por_empresa.setdefault(empresa, []).append((posicao, etapa_local))

        tx = f"tx-{self.empresa}-{uuid.uuid4().hex[:10]}"
        chave = [self.relogio.tique(), self.empresa]
        participantes = sorted(por_empresa)  # ordem canônica (anti-deadlock)
        passageiro_global = f"{usuario}@{self.empresa}"

        # O início vai ao WAL antes do primeiro PREPARE: se este coordenador
        # cair, a recuperação sabe que a transação existe e a aborta.
        self.wal.registrar({"tipo": "inicio", "tx": tx, "chave": chave,
                            "participantes": participantes,
                            "passageiro": passageiro_global})
        with self.trava:
            self.transacoes[tx] = {"estado": "pendente",
                                   "participantes": participantes,
                                   "pendentes": set()}

        # ---- Fase 1: PREPARE em ordem canônica ----
        preparados: list[str] = []
        resolvidas_por_empresa: dict[str, list[dict]] = {}
        for empresa in participantes:
            corpo = {"chave": chave, "coordenador": self.empresa,
                     "passageiro": passageiro_global,
                     "etapas": [e for _, e in por_empresa[empresa]]}
            voto = self._preparar_em(empresa, tx, corpo)
            if voto.get("voto") == "sim":
                preparados.append(empresa)
                resolvidas_por_empresa[empresa] = voto.get("etapas", [])
            else:
                motivo = voto.get("motivo", f"empresa {empresa} indisponível")
                self._decidir_abort(tx, participantes, motivo)
                raise ErroNegocio(motivo)

        # Coerência de data entre TODAS as etapas (só conhecida após resolver).
        datas = {e["data"] for lista in resolvidas_por_empresa.values() for e in lista}
        if len(datas) != 1:
            self._decidir_abort(tx, participantes, "etapas em datas diferentes")
            raise ErroNegocio("todas as etapas do itinerário devem ser na mesma data")

        # ---- Decisão: COMMIT gravado no WAL antes de qualquer envio ----
        self.wal.registrar({"tipo": "decisao", "tx": tx, "resultado": "efetivada"})
        with self.trava:
            self.transacoes[tx]["estado"] = "efetivada"
            self.transacoes[tx]["pendentes"] = set(participantes)

        # ---- Fase 2: COMMIT em todos os participantes ----
        expirados = []
        for empresa in participantes:
            ok, expirou = self._efetivar_em(empresa, tx)
            if ok:
                with self.trava:
                    self.transacoes[tx]["pendentes"].discard(empresa)
            elif expirou:
                expirados.append(empresa)
            # inalcançável: fica em pendentes; o varredor reenvia (idempotente)

        if expirados:
            # Janela rara: um participante expirou o hold após a decisão.
            # Compensa cancelando a transação inteira em todo o consórcio.
            self._compensar(tx, participantes)
            raise ErroNegocio(
                "reserva desfeita por expiração durante a confirmação; tente novamente")

        with self.trava:
            concluida = not self.transacoes[tx]["pendentes"]
        if concluida:
            self.wal.registrar({"tipo": "concluida", "tx": tx})

        # Registro do itinerário para o passageiro (lado coordenador), com as
        # etapas na ordem original da viagem e ids globais.
        etapas_finais: list[tuple[int, dict]] = []
        for empresa in participantes:
            posicoes = [pos for pos, _ in por_empresa[empresa]]
            for pos, resolvida in zip(posicoes, resolvidas_por_empresa[empresa]):
                etapa = dict(resolvida)
                etapa["carona_id"] = f"{empresa}:{etapa['carona_id']}"
                etapa["empresa"] = empresa
                etapa["motorista"] = f"{etapa['motorista']}@{empresa}"
                etapas_finais.append((pos, etapa))
        etapas_finais.sort(key=lambda par: par[0])
        etapas_ordenadas = [e for _, e in etapas_finais]

        registro = {
            "id": tx,
            "tx": tx,
            "participantes": participantes,
            "passageiro": usuario,
            "status": "confirmada",
            "criada_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "data": etapas_ordenadas[0]["data"],
            "etapas": etapas_ordenadas,
            "preco_total": round(sum(e["preco"] for e in etapas_ordenadas), 2),
        }
        self.estado.registrar_itinerario(registro)
        return registro

    def _preparar_em(self, empresa: str, tx: str, corpo: dict) -> dict:
        """PREPARE com repetição em caso de 'aguarde' (wait-die: a transação
        mais antiga espera a mais nova terminar ou morrer)."""
        for _ in range(TENTATIVAS_AGUARDE):
            if empresa == self.empresa:
                try:
                    voto = self.estado.preparar_hold(
                        tx, corpo["etapas"], corpo["chave"],
                        corpo["coordenador"], corpo["passageiro"])
                except ErroNegocio as exc:
                    return {"voto": "nao", "motivo": str(exc)}
            else:
                status, dados = self.pares.preparar(empresa, tx, corpo)
                if status is None:
                    return {"voto": "nao",
                            "motivo": f"empresa {empresa} indisponível"}
                voto = dados if isinstance(dados, dict) else {"voto": "nao"}
            if voto.get("voto") != "aguarde":
                return voto
            time.sleep(INTERVALO_AGUARDE)
        return {"voto": "nao", "motivo": "disputa persistente pelo assento (aguarde excedido)"}

    def _efetivar_em(self, empresa: str, tx: str) -> tuple[bool, bool]:
        """COMMIT em um participante. Devolve (ok, hold_expirado)."""
        if empresa == self.empresa:
            reserva = self.estado.efetivar_hold(tx)
            return (reserva is not None), (reserva is None)
        status, _ = self.pares.efetivar(empresa, tx)
        if status is None:
            return False, False  # inalcançável: reenvio posterior
        return status == 200, status == 410

    def _abortar_em(self, empresa: str, tx: str) -> bool:
        if empresa == self.empresa:
            self.estado.abortar_hold(tx)
            return True
        status, _ = self.pares.abortar(empresa, tx)
        return status is not None

    def _decidir_abort(self, tx: str, participantes: list[str], motivo: str) -> None:
        """Grava a decisão de aborto e dispara os ABORTs (idempotentes)."""
        self.wal.registrar({"tipo": "decisao", "tx": tx,
                            "resultado": "abortada", "motivo": motivo})
        with self.trava:
            self.transacoes[tx]["estado"] = "abortada"
            self.transacoes[tx]["pendentes"] = set(participantes)
        for empresa in participantes:
            if self._abortar_em(empresa, tx):
                with self.trava:
                    self.transacoes[tx]["pendentes"].discard(empresa)
        with self.trava:
            concluida = not self.transacoes[tx]["pendentes"]
        if concluida:
            self.wal.registrar({"tipo": "concluida", "tx": tx})

    def _compensar(self, tx: str, participantes: list[str]) -> None:
        """Desfaz uma transação JÁ decidida como commit (janela rara em que
        um participante presumiu aborto): cancela a reserva em todos — a
        consistência global prevalece sobre a confirmação parcial."""
        self.wal.registrar({"tipo": "compensada", "tx": tx})
        with self.trava:
            self.transacoes[tx]["estado"] = "compensada"
            self.transacoes[tx]["pendentes"] = set()
        for empresa in participantes:
            if empresa == self.empresa:
                self.estado.cancelar_reserva_tx(tx)
            else:
                status, _ = self.pares.cancelar_reserva(empresa, tx)
                if status is None:
                    with self.trava:
                        self.pendencias_cancel.append((empresa, tx))
        self.estado.marcar_itinerario(tx, "cancelada_por_falha")

    # ------------------------------------------------------------------
    # COORDENADOR: cancelamento distribuído e notificações
    # ------------------------------------------------------------------
    def cancelar_itinerario(self, usuario: str, reserva_id: str) -> dict:
        with self.estado.trava_global:
            registro = self.estado.reservas.get(reserva_id)
        if registro is None or registro.get("passageiro") != usuario:
            raise ErroNegocio("reserva não encontrada")
        if registro.get("tx") is None:
            # Registro antigo (Problema 1): caminho local.
            return self.estado.cancelar_reserva(usuario, reserva_id)
        if registro["status"] != "confirmada":
            raise ErroNegocio(f"reserva não está confirmada (status: {registro['status']})")

        tx = registro["tx"]
        for empresa in registro.get("participantes", []):
            if empresa == self.empresa:
                self.estado.cancelar_reserva_tx(tx)
            else:
                status, _ = self.pares.cancelar_reserva(empresa, tx)
                if status is None:
                    # Par fora do ar: o varredor insiste até conseguir.
                    with self.trava:
                        self.pendencias_cancel.append((empresa, tx))
        self.estado.marcar_itinerario(reserva_id, "cancelada")
        return {"reserva_id": reserva_id, "status": "cancelada"}

    def notificar_cancelamentos_de_carona(self, transacoes_afetadas) -> None:
        """Após um motorista local cancelar a carona, avisa (melhor esforço)
        os coordenadores das transações afetadas para que cancelem o
        itinerário inteiro nas demais empresas."""
        for tx, coordenador in transacoes_afetadas:
            if coordenador == self.empresa:
                self.tratar_notificacao_cancelamento(tx, "cancelada_pelo_motorista")
            else:
                self.pares.notificar_cancelamento(coordenador, tx,
                                                  "cancelada_pelo_motorista")

    def tratar_notificacao_cancelamento(self, tx: str, motivo: str) -> None:
        """Lado coordenador: uma empresa cancelou a carona de uma etapa ->
        cancela o itinerário inteiro (a atomicidade vale também no desfazer)."""
        with self.trava:
            info = self.transacoes.get(tx)
        participantes = (info or {}).get("participantes", [])
        for empresa in participantes:
            if empresa == self.empresa:
                self.estado.cancelar_reserva_tx(tx)
            else:
                status, _ = self.pares.cancelar_reserva(empresa, tx)
                if status is None:
                    with self.trava:
                        self.pendencias_cancel.append((empresa, tx))
        self.estado.marcar_itinerario(tx, motivo)

    # ------------------------------------------------------------------
    # Consulta de status (usada pelos participantes para resolver holds)
    # ------------------------------------------------------------------
    def status_transacao(self, tx: str) -> str:
        with self.trava:
            info = self.transacoes.get(tx)
        if info is None:
            return "desconhecida"
        if info["estado"] == "compensada":
            return "abortada"
        return info["estado"]  # pendente | efetivada | abortada

    # ------------------------------------------------------------------
    # Varredor: leases, reenvios e pendências (thread de fundo)
    # ------------------------------------------------------------------
    def _varredor(self) -> None:
        while True:
            time.sleep(2)
            try:
                self._resolver_holds_vencidos()
                self._reenviar_pendencias()
                self._retentar_cancelamentos()
            except Exception as exc:  # noqa: BLE001 - varredor nunca morre
                print(f"[consorcio] varredor: erro ignorado: {exc!r}")

    def _resolver_holds_vencidos(self) -> None:
        """Um hold vencido NÃO é liberado às cegas: o participante consulta o
        coordenador (pull). 'efetivada' -> aplica o commit que se perdeu;
        'abortada'/'desconhecida' -> libera; coordenador fora do ar -> estende
        o lease até a carência e então presume aborto (nenhum assento fica
        preso indefinidamente — requisito do problema)."""
        for hold in self.estado.holds_vencidos():
            tx, coordenador = hold["tx"], hold["coordenador"]
            if coordenador == self.empresa:
                situacao = self.status_transacao(tx)
            else:
                status, dados = self.pares.status_transacao(coordenador, tx)
                if status is None:
                    inicio = self._primeiro_vencimento.setdefault(tx, time.time())
                    if time.time() - inicio > CARENCIA_PRESUNCAO:
                        print(f"[consorcio] {tx}: coordenador {coordenador} "
                              "inalcançável além da carência; presumindo aborto")
                        self.estado.abortar_hold(tx, "presunção de aborto")
                        self._primeiro_vencimento.pop(tx, None)
                    else:
                        self.estado.estender_hold(tx, 5)
                    continue
                situacao = (dados or {}).get("estado", "desconhecida")

            self._primeiro_vencimento.pop(tx, None)
            if situacao == "efetivada":
                self.estado.efetivar_hold(tx)
            elif situacao == "pendente":
                self.estado.estender_hold(tx, LEASE_SEGUNDOS / 2)
            else:  # abortada / desconhecida
                self.estado.abortar_hold(tx, "resolvido via coordenador")

    def _reenviar_pendencias(self) -> None:
        """Reenvia COMMITs/ABORTs que não chegaram (participante estava fora
        do ar). As operações são idempotentes, então reenviar é sempre seguro."""
        with self.trava:
            itens = [(tx, dict(info)) for tx, info in self.transacoes.items()
                     if info["estado"] in ("efetivada", "abortada") and info["pendentes"]]
        for tx, info in itens:
            for empresa in list(info["pendentes"]):
                if info["estado"] == "efetivada":
                    ok, expirou = self._efetivar_em(empresa, tx)
                    if expirou:
                        self._compensar(tx, info["participantes"])
                        break
                else:
                    ok = self._abortar_em(empresa, tx)
                if ok:
                    with self.trava:
                        self.transacoes[tx]["pendentes"].discard(empresa)
            with self.trava:
                concluida = (self.transacoes[tx]["estado"] in ("efetivada", "abortada")
                             and not self.transacoes[tx]["pendentes"])
            if concluida:
                self.wal.registrar({"tipo": "concluida", "tx": tx})

    def _retentar_cancelamentos(self) -> None:
        with self.trava:
            pendentes, self.pendencias_cancel = self.pendencias_cancel, []
        restantes = []
        for empresa, tx in pendentes:
            status, _ = self.pares.cancelar_reserva(empresa, tx)
            if status is None:
                restantes.append((empresa, tx))
        with self.trava:
            self.pendencias_cancel.extend(restantes)

    # ------------------------------------------------------------------
    # Recuperação após queda (releitura do WAL)
    # ------------------------------------------------------------------
    def _recuperar(self) -> None:
        """Reconstrói o registro de transações do WAL e termina o que ficou
        no meio: transação sem decisão é ABORTADA (presume abort); decisão
        sem conclusão tem os COMMITs/ABORTs reenviados pelo varredor."""
        for registro in self.wal.ler_tudo():
            tx = registro.get("tx")
            tipo = registro.get("tipo")
            if tipo == "inicio":
                self.transacoes[tx] = {"estado": "pendente",
                                       "participantes": registro["participantes"],
                                       "pendentes": set()}
            elif tipo == "decisao" and tx in self.transacoes:
                self.transacoes[tx]["estado"] = registro["resultado"]
                self.transacoes[tx]["pendentes"] = set(
                    self.transacoes[tx]["participantes"])
            elif tipo == "concluida" and tx in self.transacoes:
                self.transacoes[tx]["pendentes"] = set()
            elif tipo == "compensada" and tx in self.transacoes:
                self.transacoes[tx]["estado"] = "compensada"
                self.transacoes[tx]["pendentes"] = set()

        pendentes_de_decisao = [tx for tx, info in self.transacoes.items()
                                if info["estado"] == "pendente"]
        for tx in pendentes_de_decisao:
            print(f"[consorcio] recuperação: {tx} sem decisão -> abortando")
            self._decidir_abort(tx, self.transacoes[tx]["participantes"],
                                "coordenador reiniciou antes de decidir")


def mapa_de_pares_do_ambiente() -> dict[str, str]:
    """Lê PARES="empresaB=http://host:porta,empresaC=http://host:porta"."""
    bruto = os.environ.get("PARES", "").strip()
    mapa = {}
    if bruto:
        for item in bruto.split(","):
            if "=" in item:
                nome, url = item.split("=", 1)
                mapa[nome.strip()] = url.strip()
    return mapa
