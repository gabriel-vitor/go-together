"""
Estado central do VAIJUNTO: caronas, reservas e usuários.

Arquitetura de dados
--------------------
O "banco de dados" do sistema é a memória do processo do servidor, protegida
por travas (locks). O arquivo JSON em disco é apenas PERSISTÊNCIA: um
snapshot do estado gravado de forma atômica após cada mutação, recarregado
quando o servidor reinicia. Essa separação existe porque o controle de
concorrência é responsabilidade da própria aplicação (restrição do problema),
e disputar um arquivo em disco a cada requisição seria lento e frágil.

Modelo de dados
---------------
* Carona: rota = sequência ordenada de N cidades, que define N-1 TRECHOS
  (trecho k liga rota[k] a rota[k+1]). A disponibilidade é controlada POR
  TRECHO: `ocupados[k]` conta os assentos tomados no trecho k. Um passageiro
  que viaja da cidade i à cidade j ocupa 1 assento em cada trecho k com
  i <= k < j — e apenas nesses; o assento volta a estar livre nos demais.
* Reserva: um itinerário confirmado, composto por 1..4 "etapas", cada etapa
  sendo um sub-percurso contíguo de UMA carona. Etapas de caronas de
  motoristas diferentes podem compor a mesma reserva.

Controle de concorrência
------------------------
* `trava_global` protege os dicionários (criação/consulta de usuários,
  caronas e reservas) — seção crítica curta, só para tocar nos índices.
* Cada carona tem sua PRÓPRIA trava (`travas_caronas[id]`), permitindo que
  reservas em caronas distintas prossigam em paralelo.
* Reserva de itinerário multi-carona: as travas de todas as caronas
  envolvidas são adquiridas EM ORDEM GLOBAL (id crescente). Como todo
  thread respeita a mesma ordem total de aquisição, é impossível formar o
  ciclo de espera necessário para um deadlock, mesmo quando dois passageiros
  reservam as mesmas caronas em ordens de viagem diferentes.
* Atomicidade: dentro da seção crítica, TODAS as verificações de
  disponibilidade acontecem antes de QUALQUER escrita. Se um único trecho
  não tem assento, uma exceção aborta a operação antes do primeiro
  incremento — ou o itinerário inteiro é confirmado, ou nada é.
"""

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

# Limites de sanidade nas entradas (defesa contra clientes defeituosos).
MAX_CIDADES_ROTA = 20
MAX_ASSENTOS = 50
MAX_ETAPAS_RESERVA = 4

# Problema 2: tempo de vida (lease) de uma reserva temporária (hold) criada
# na fase PREPARE do 2PC. Se o coordenador sumir sem decidir, o hold expira
# e os assentos voltam — nenhum assento fica bloqueado indefinidamente.
LEASE_SEGUNDOS = int(os.environ.get("VAIJUNTO_LEASE", "30"))


class ErroNegocio(Exception):
    """Violação de regra de negócio; vira resposta {"status": "erro"} para o
    cliente, sem derrubar a conexão nem o servidor."""


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hash_senha(senha: str) -> str:
    # Armazenamos apenas o hash da senha, nunca o texto puro.
    return hashlib.sha256(senha.encode("utf-8")).hexdigest()


def _novo_id(prefixo: str) -> str:
    return f"{prefixo}-{uuid.uuid4().hex[:10]}"


def _exigir_texto(valor, campo: str, max_tamanho: int = 100) -> str:
    """Valida que `valor` é uma string não vazia e devolve-a normalizada."""
    if not isinstance(valor, str) or not valor.strip():
        raise ErroNegocio(f"campo '{campo}' ausente ou inválido")
    valor = valor.strip()
    if len(valor) > max_tamanho:
        raise ErroNegocio(f"campo '{campo}' excede {max_tamanho} caracteres")
    return valor


def _validar_data(data: str) -> str:
    try:
        datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        raise ErroNegocio("data deve estar no formato AAAA-MM-DD") from None
    return data


def _validar_hora(hora: str) -> str:
    try:
        datetime.strptime(hora, "%H:%M")
    except ValueError:
        raise ErroNegocio("hora deve estar no formato HH:MM") from None
    return hora


class Estado:
    """Estado compartilhado do servidor, com travas e persistência em JSON."""

    def __init__(self, caminho_arquivo: str = os.path.join("dados", "estado.json")):
        self.caminho_arquivo = caminho_arquivo
        self.trava_global = threading.RLock()
        self.trava_persistencia = threading.Lock()
        self.usuarios: dict[str, dict] = {}
        self.caronas: dict[str, dict] = {}
        self.reservas: dict[str, dict] = {}
        self.travas_caronas: dict[str, threading.Lock] = {}
        # Problema 2 — papel de PARTICIPANTE do 2PC:
        # holds[tx] = reserva temporária (assentos retidos na fase PREPARE,
        # aguardando COMMIT/ABORT do coordenador, com lease de expiração).
        self.holds: dict[str, dict] = {}
        # desfechos[tx] = resultado final de transações já resolvidas aqui
        # ("efetivada"/"abortada" + reserva_id), para IDEMPOTÊNCIA: repetir
        # um commit/abort devolve o mesmo resultado, nunca aplica duas vezes.
        self.desfechos: dict[str, dict] = {}
        self._carregar()

    # ------------------------------------------------------------------
    # Travamento ordenado (prevenção de deadlock)
    # ------------------------------------------------------------------
    @contextmanager
    def _travar_caronas(self, ids: list[str]):
        """Adquire as travas das caronas indicadas SEMPRE em ordem crescente
        de id. A ordem total de aquisição elimina a condição de espera
        circular, um dos quatro requisitos para deadlock (Coffman)."""
        ids_ordenados = sorted(set(ids))
        with self.trava_global:
            travas = [self.travas_caronas[i] for i in ids_ordenados]
        for trava in travas:
            trava.acquire()
        try:
            yield
        finally:
            # Liberação em ordem inversa (boa prática; a ordem de liberação
            # não afeta a correção, apenas a de aquisição).
            for trava in reversed(travas):
                trava.release()

    # ------------------------------------------------------------------
    # Usuários
    # ------------------------------------------------------------------
    def registrar_usuario(self, nome, senha) -> None:
        nome = _exigir_texto(nome, "usuario", 40)
        if not isinstance(senha, str) or len(senha) < 4:
            raise ErroNegocio("a senha deve ter pelo menos 4 caracteres")
        with self.trava_global:
            if nome in self.usuarios:
                raise ErroNegocio(f"usuário '{nome}' já existe")
            self.usuarios[nome] = {"senha": _hash_senha(senha), "criado_em": _agora()}
        self._salvar()

    def autenticar(self, nome, senha) -> str:
        nome = _exigir_texto(nome, "usuario", 40)
        if not isinstance(senha, str):
            raise ErroNegocio("campo 'senha' ausente ou inválido")
        with self.trava_global:
            registro = self.usuarios.get(nome)
        if registro is None or registro["senha"] != _hash_senha(senha):
            raise ErroNegocio("usuário ou senha incorretos")
        return nome

    # ------------------------------------------------------------------
    # Caronas (lado motorista)
    # ------------------------------------------------------------------
    def publicar_carona(self, motorista, rota, data, hora, assentos, precos) -> dict:
        # --- validação completa ANTES de tocar no estado compartilhado ---
        if not isinstance(rota, list) or len(rota) < 2 or len(rota) > MAX_CIDADES_ROTA:
            raise ErroNegocio(f"rota deve ser uma lista de 2 a {MAX_CIDADES_ROTA} cidades")
        cidades = [_exigir_texto(c, "cidade da rota", 60) for c in rota]
        vistos = set()
        for c in cidades:
            chave = c.casefold()
            if chave in vistos:
                raise ErroNegocio(f"cidade repetida na rota: {c}")
            vistos.add(chave)

        data = _validar_data(_exigir_texto(data, "data", 10))
        hora = _validar_hora(_exigir_texto(hora, "hora", 5))

        if not isinstance(assentos, int) or not (1 <= assentos <= MAX_ASSENTOS):
            raise ErroNegocio(f"assentos deve ser inteiro entre 1 e {MAX_ASSENTOS}")

        n_trechos = len(cidades) - 1
        if not isinstance(precos, list) or len(precos) != n_trechos:
            raise ErroNegocio(f"precos deve ser uma lista com {n_trechos} valores (um por trecho)")
        precos_validados = []
        for p in precos:
            if not isinstance(p, (int, float)) or isinstance(p, bool) or p < 0 or p > 100000:
                raise ErroNegocio("cada preço deve ser um número entre 0 e 100000")
            precos_validados.append(round(float(p), 2))

        carona = {
            "id": _novo_id("car"),
            "motorista": motorista,
            "rota": cidades,
            "data": data,
            "hora": hora,
            "assentos": assentos,
            "precos": precos_validados,
            # ocupados[k]: assentos tomados no trecho rota[k] -> rota[k+1]
            "ocupados": [0] * n_trechos,
            "cancelada": False,
            "criada_em": _agora(),
        }
        with self.trava_global:
            self.caronas[carona["id"]] = carona
            self.travas_caronas[carona["id"]] = threading.Lock()
        self._salvar()
        return self._visao_carona(carona)

    def minhas_caronas(self, motorista) -> list[dict]:
        """Caronas do motorista, com os passageiros confirmados POR TRECHO."""
        with self.trava_global:
            minhas = [c for c in self.caronas.values() if c["motorista"] == motorista]
            reservas = list(self.reservas.values())
        visoes = []
        for carona in sorted(minhas, key=lambda c: (c["data"], c["hora"])):
            with self.travas_caronas[carona["id"]]:
                visao = self._visao_carona(carona)
                # Para cada trecho, lista quem o ocupa (derivado das reservas
                # confirmadas cujas etapas cobrem o trecho k desta carona).
                for k, trecho in enumerate(visao["trechos"]):
                    trecho["passageiros"] = sorted(
                        r["passageiro"]
                        for r in reservas
                        if r["status"] == "confirmada"
                        for etapa in r["etapas"]
                        if etapa["carona_id"] == carona["id"]
                        and etapa["idx_origem"] <= k < etapa["idx_destino"]
                    )
            visoes.append(visao)
        return visoes

    def cancelar_carona(self, motorista, carona_id) -> dict:
        """Cancela uma carona do motorista e TODAS as reservas que a usavam.

        Ordem das operações (importante para a correção):
          1. marca a carona como cancelada sob a trava dela — a partir daí
             nenhuma nova reserva a aceita;
          2. cancela cada reserva afetada por inteiro (os assentos das outras
             caronas do itinerário também são liberados, pois o itinerário é
             atômico: sem uma etapa, as demais perdem o sentido).
        """
        carona_id = _exigir_texto(carona_id, "carona_id", 40)
        with self.trava_global:
            carona = self.caronas.get(carona_id)
            if carona is None:
                raise ErroNegocio("carona não encontrada")
            if carona["motorista"] != motorista:
                raise ErroNegocio("apenas o motorista dono pode cancelar a carona")
            trava = self.travas_caronas[carona_id]

        with trava:
            if carona["cancelada"]:
                raise ErroNegocio("carona já estava cancelada")
            carona["cancelada"] = True

        # Coleta as reservas confirmadas que usavam esta carona.
        with self.trava_global:
            afetadas = [
                r for r in self.reservas.values()
                if r["status"] == "confirmada"
                and any(p["carona_id"] == carona_id for p in r["etapas"])
            ]
        for reserva in afetadas:
            self._desfazer_reserva(reserva, novo_status="cancelada_pelo_motorista")

        # Problema 2: transações distribuídas afetadas (tx + coordenador),
        # para o consórcio notificar os coordenadores — que cancelam o
        # itinerário inteiro também nas demais empresas.
        transacoes_afetadas = [
            (r["tx"], r.get("coordenador"))
            for r in afetadas if r.get("tx") and r.get("coordenador")
        ]
        self._salvar()
        return {"carona_id": carona_id, "reservas_canceladas": len(afetadas),
                "transacoes_afetadas": transacoes_afetadas}

    # ------------------------------------------------------------------
    # Reservas (lado passageiro)
    # ------------------------------------------------------------------
    def _resolver_etapas(self, etapas, exigir_contiguidade: bool = True):
        """Valida a estrutura das etapas e resolve-as contra as caronas LOCAIS.

        Devolve (caronas, demanda, etapas_resolvidas), onde `demanda` mapeia
        (carona_id, trecho) -> assentos necessários. Não adquire travas de
        carona: é a fase de validação, compartilhada entre a reserva local
        (Problema 1) e o PREPARE do 2PC (Problema 2). No 2PC, um participante
        recebe apenas as etapas SUAS, que podem não ser contíguas entre si
        (a contiguidade do itinerário completo é verificada pelo coordenador).
        """
        if not isinstance(etapas, list) or not (1 <= len(etapas) <= MAX_ETAPAS_RESERVA):
            raise ErroNegocio(f"etapas deve ser uma lista de 1 a {MAX_ETAPAS_RESERVA} itens")

        with self.trava_global:
            caronas = {}
            for p in etapas:
                if not isinstance(p, dict):
                    raise ErroNegocio("cada etapa deve ser um objeto")
                cid = _exigir_texto(p.get("carona_id"), "carona_id", 80)
                carona = self.caronas.get(cid)
                if carona is None:
                    raise ErroNegocio(f"carona '{cid}' não encontrada")
                caronas[cid] = carona

        demanda: dict[tuple[str, int], int] = {}  # (carona_id, trecho) -> qtd
        etapas_resolvidas = []
        datas = set()
        for p in etapas:
            cid = p["carona_id"].strip()
            carona = caronas[cid]
            origem = _exigir_texto(p.get("origem"), "origem", 60)
            destino = _exigir_texto(p.get("destino"), "destino", 60)
            rota_norm = [c.casefold() for c in carona["rota"]]
            try:
                idx_o = rota_norm.index(origem.casefold())
                idx_d = rota_norm.index(destino.casefold())
            except ValueError:
                raise ErroNegocio(
                    f"a carona {cid} não passa por '{origem}' e/ou '{destino}'") from None
            if idx_o >= idx_d:
                raise ErroNegocio(
                    f"na carona {cid}, '{origem}' não vem antes de '{destino}' na rota")
            for k in range(idx_o, idx_d):
                demanda[(cid, k)] = demanda.get((cid, k), 0) + 1
            preco = round(sum(carona["precos"][idx_o:idx_d]), 2)
            datas.add(carona["data"])
            etapas_resolvidas.append({
                "carona_id": cid,
                "motorista": carona["motorista"],
                "origem": carona["rota"][idx_o],
                "destino": carona["rota"][idx_d],
                "idx_origem": idx_o,
                "idx_destino": idx_d,
                "data": carona["data"],
                "hora": carona["hora"],
                "preco": preco,
            })

        if exigir_contiguidade:
            # O itinerário deve ser contíguo: o destino de uma etapa é a
            # origem da seguinte, e todas as etapas na mesma data.
            for anterior, seguinte in zip(etapas_resolvidas, etapas_resolvidas[1:]):
                if anterior["destino"].casefold() != seguinte["origem"].casefold():
                    raise ErroNegocio(
                        "itinerário descontínuo: o destino de uma etapa deve ser a origem da próxima")
        if len(datas) != 1:
            raise ErroNegocio("todas as etapas do itinerário devem ser na mesma data")
        return caronas, demanda, etapas_resolvidas

    def reservar(self, passageiro, etapas) -> dict:
        """Confirma atomicamente um itinerário LOCAL (caminho do Problema 1).

        Fase 1 (sem travas de carona): valida e resolve via _resolver_etapas.
        Fase 2 (seção crítica): trava as caronas em ordem global de id,
        re-verifica TODA a disponibilidade e só então grava. Qualquer falha
        aborta antes da primeira escrita — tudo ou nada.
        """
        caronas, demanda, etapas_resolvidas = self._resolver_etapas(etapas)

        # --- Fase 2: seção crítica com travas em ordem global ---
        with self._travar_caronas(list(caronas)):
            for carona in caronas.values():
                if carona["cancelada"]:
                    raise ErroNegocio(f"a carona {carona['id']} foi cancelada pelo motorista")
            # Verifica TUDO antes de escrever QUALQUER coisa (atomicidade).
            for (cid, k), qtd in demanda.items():
                carona = caronas[cid]
                if carona["ocupados"][k] + qtd > carona["assentos"]:
                    raise ErroNegocio(
                        "assentos esgotados no trecho "
                        f"{carona['rota'][k]} -> {carona['rota'][k + 1]} (carona {cid})")
            # Todas as verificações passaram: efetiva a reserva.
            for (cid, k), qtd in demanda.items():
                caronas[cid]["ocupados"][k] += qtd
            reserva = {
                "id": _novo_id("res"),
                "passageiro": passageiro,
                "status": "confirmada",
                "criada_em": _agora(),
                "data": etapas_resolvidas[0]["data"],
                "etapas": etapas_resolvidas,
                "preco_total": round(sum(p["preco"] for p in etapas_resolvidas), 2),
            }
            with self.trava_global:
                self.reservas[reserva["id"]] = reserva

        self._salvar()
        return reserva

    def minhas_reservas(self, passageiro) -> list[dict]:
        with self.trava_global:
            minhas = [r for r in self.reservas.values() if r["passageiro"] == passageiro]
        return sorted(minhas, key=lambda r: r["criada_em"], reverse=True)

    def cancelar_reserva(self, passageiro, reserva_id) -> dict:
        reserva_id = _exigir_texto(reserva_id, "reserva_id", 40)
        with self.trava_global:
            reserva = self.reservas.get(reserva_id)
        if reserva is None or reserva["passageiro"] != passageiro:
            raise ErroNegocio("reserva não encontrada")
        if reserva["status"] != "confirmada":
            raise ErroNegocio(f"reserva não está confirmada (status: {reserva['status']})")
        self._desfazer_reserva(reserva, novo_status="cancelada")
        self._salvar()
        return {"reserva_id": reserva_id, "status": "cancelada"}

    def _desfazer_reserva(self, reserva: dict, novo_status: str) -> None:
        """Libera os assentos de todas as etapas da reserva e muda o status.

        Usa o mesmo travamento ordenado da confirmação; a checagem do status
        dentro da seção crítica evita liberação dupla se dois cancelamentos
        concorrerem sobre a mesma reserva.
        """
        ids = [p["carona_id"] for p in reserva["etapas"]]
        with self._travar_caronas(ids):
            if reserva["status"] != "confirmada":
                return  # outro thread já desfez esta reserva
            reserva["status"] = novo_status
            with self.trava_global:
                caronas = {i: self.caronas[i] for i in ids}
            for etapa in reserva["etapas"]:
                carona = caronas[etapa["carona_id"]]
                for k in range(etapa["idx_origem"], etapa["idx_destino"]):
                    carona["ocupados"][k] -= 1

    # ------------------------------------------------------------------
    # Problema 2 — papel de PARTICIPANTE do 2PC (holds com lease)
    # ------------------------------------------------------------------
    # Princípio do DONO ÚNICO: cada carona pertence a UMA empresa, e só o
    # servidor dela escreve em `ocupados[]` — sempre sob a trava local da
    # carona. A exclusão mútua distribuída reduz-se, assim, à exclusão
    # mútua local: não existe trava distribuída sobre o assento.
    #
    # PREPARE cria um "hold": os assentos são retidos (ocupados[] já é
    # incrementado, impedindo venda dupla) mas a reserva ainda não existe.
    # COMMIT converte o hold em reserva; ABORT (ou a expiração do lease)
    # devolve os assentos. Tudo idempotente, chaveado pelo id da transação.

    def preparar_hold(self, tx_id, etapas, chave, coordenador, passageiro) -> dict:
        """Fase 1 do 2PC no participante: tenta reter os assentos pedidos.

        `chave` = [relogio_lamport, empresa] da transação: a ordem TOTAL que
        define a precedência entre transações concorrentes, independente dos
        relógios físicos dos servidores.

        Votos possíveis:
          * "sim"     -> assentos retidos (hold criado, lease correndo);
          * "nao"     -> indisponível em definitivo (reservas confirmadas);
          * "aguarde" -> indisponível por holds de transações MAIS NOVAS
                         (chave maior). Pelo esquema wait-die, a transação
                         mais antiga espera e repete; a mais nova morre.
                         Isso dá um critério de precedência determinístico
                         e elimina deadlock distribuído.
        """
        tx_id = _exigir_texto(tx_id, "tx", 80)
        if not (isinstance(chave, list) and len(chave) == 2):
            raise ErroNegocio("chave de precedência inválida")

        # Idempotência: PREPARE repetido devolve o resultado já conhecido.
        with self.trava_global:
            if tx_id in self.holds:
                return {"voto": "sim", "etapas": self.holds[tx_id]["etapas"]}
            desfecho = self.desfechos.get(tx_id)
        if desfecho is not None:
            if desfecho["resultado"] == "efetivada":
                return {"voto": "sim", "etapas": desfecho.get("etapas", [])}
            return {"voto": "nao", "motivo": "transação já abortada"}

        caronas, demanda, etapas_resolvidas = self._resolver_etapas(
            etapas, exigir_contiguidade=False)

        with self._travar_caronas(list(caronas)):
            for carona in caronas.values():
                if carona["cancelada"]:
                    return {"voto": "nao",
                            "motivo": f"a carona {carona['id']} foi cancelada pelo motorista"}

            # Verifica a disponibilidade de TODOS os trechos antes de reter
            # qualquer assento (mesma disciplina tudo-ou-nada do Problema 1).
            faltantes = []
            for (cid, k), qtd in demanda.items():
                carona = caronas[cid]
                if carona["ocupados"][k] + qtd > carona["assentos"]:
                    faltantes.append((cid, k, qtd))

            if faltantes:
                # Wait-die: se liberar os holds de transações MAIS NOVAS
                # (chave maior) bastaria, a transação mais antiga AGUARDA;
                # caso contrário o esgotamento é definitivo e o voto é não.
                with self.trava_global:
                    holds_ativos = list(self.holds.values())
                for (cid, k, qtd) in faltantes:
                    retidos_por_novas = 0
                    for h in holds_ativos:
                        if list(h["chave"]) > list(chave):
                            retidos_por_novas += sum(
                                q for (hc, hk), q in
                                (((d[0], d[1]), d[2]) for d in h["demanda"])
                                if hc == cid and hk == k)
                    carona = caronas[cid]
                    if carona["ocupados"][k] - retidos_por_novas + qtd > carona["assentos"]:
                        return {"voto": "nao",
                                "motivo": "assentos esgotados no trecho "
                                          f"{carona['rota'][k]} -> {carona['rota'][k + 1]}"}
                return {"voto": "aguarde"}

            # Tudo disponível: retém os assentos e registra o hold.
            for (cid, k), qtd in demanda.items():
                caronas[cid]["ocupados"][k] += qtd
            hold = {
                "tx": tx_id,
                "chave": list(chave),
                "coordenador": coordenador,
                "passageiro": passageiro,
                # demanda serializada como listas (JSON não tem tuplas)
                "demanda": [[cid, k, qtd] for (cid, k), qtd in demanda.items()],
                "etapas": etapas_resolvidas,
                "criado_em": _agora(),
                "expira_em": time.time() + LEASE_SEGUNDOS,
            }
            with self.trava_global:
                self.holds[tx_id] = hold
        self._salvar()
        return {"voto": "sim", "etapas": etapas_resolvidas}

    def efetivar_hold(self, tx_id) -> dict | None:
        """Fase 2 do 2PC (COMMIT): converte o hold em reserva confirmada.

        Idempotente: repetir devolve a mesma reserva. Devolve None se a
        transação é desconhecida ou o hold já expirou/abortou — o chamador
        traduz isso em 410 (Gone) para o coordenador tratar.
        """
        with self.trava_global:
            desfecho = self.desfechos.get(tx_id)
            if desfecho is not None:
                if desfecho["resultado"] == "efetivada":
                    return self.reservas.get(desfecho["reserva_id"])
                return None  # já abortada: tarde demais para efetivar
            hold = self.holds.get(tx_id)
        if hold is None:
            return None

        ids = sorted({d[0] for d in hold["demanda"]})
        with self._travar_caronas(ids):
            with self.trava_global:
                if tx_id not in self.holds:  # corrida com expiração/abort
                    desfecho = self.desfechos.get(tx_id)
                    if desfecho and desfecho["resultado"] == "efetivada":
                        return self.reservas.get(desfecho["reserva_id"])
                    return None
                hold = self.holds.pop(tx_id)
                # Os assentos já estão retidos em ocupados[]: basta criar a
                # reserva (o registro que o motorista enxerga por trecho).
                reserva = {
                    "id": _novo_id("res"),
                    "tx": tx_id,
                    "coordenador": hold["coordenador"],
                    "passageiro": hold["passageiro"],
                    "status": "confirmada",
                    "criada_em": _agora(),
                    "data": hold["etapas"][0]["data"],
                    "etapas": hold["etapas"],
                    "preco_total": round(sum(p["preco"] for p in hold["etapas"]), 2),
                }
                self.reservas[reserva["id"]] = reserva
                self.desfechos[tx_id] = {
                    "resultado": "efetivada",
                    "reserva_id": reserva["id"],
                    "etapas": hold["etapas"],
                }
        self._salvar()
        return reserva

    def abortar_hold(self, tx_id, motivo: str = "abortada") -> bool:
        """Fase 2 do 2PC (ABORT) e expiração de lease: devolve os assentos.

        Idempotente: abortar o que não existe (ou já foi abortado) é um
        sucesso silencioso. Abortar o que já foi EFETIVADO não desfaz a
        reserva (isso é cancelamento, outra operação) e devolve False.
        """
        with self.trava_global:
            desfecho = self.desfechos.get(tx_id)
            if desfecho is not None:
                return desfecho["resultado"] != "efetivada"
            hold = self.holds.get(tx_id)
        if hold is None:
            # Desconhecida: registra o desfecho para que um PREPARE atrasado
            # desta transação seja recusado (presume abort).
            with self.trava_global:
                self.desfechos.setdefault(tx_id, {"resultado": "abortada", "motivo": motivo})
            return True

        ids = sorted({d[0] for d in hold["demanda"]})
        with self._travar_caronas(ids):
            with self.trava_global:
                hold = self.holds.pop(tx_id, None)
                if hold is None:
                    desfecho = self.desfechos.get(tx_id)
                    return not (desfecho and desfecho["resultado"] == "efetivada")
                caronas = {i: self.caronas[i] for i in ids}
                self.desfechos[tx_id] = {"resultado": "abortada", "motivo": motivo}
            for cid, k, qtd in hold["demanda"]:
                caronas[cid]["ocupados"][k] -= qtd
        self._salvar()
        return True

    def holds_vencidos(self) -> list[dict]:
        """Holds cujo lease expirou (candidatos a resolução pelo varredor)."""
        agora = time.time()
        with self.trava_global:
            return [dict(h) for h in self.holds.values() if h["expira_em"] <= agora]

    def estender_hold(self, tx_id, segundos: float) -> None:
        """Prorroga o lease (usado quando o coordenador está inalcançável e
        ainda não se esgotou a carência de presunção de aborto)."""
        with self.trava_global:
            hold = self.holds.get(tx_id)
            if hold is not None:
                hold["expira_em"] = time.time() + segundos

    def cancelar_reserva_tx(self, tx_id) -> dict:
        """Cancela (pós-commit) a reserva local criada pela transação tx_id.

        Usado pelo cancelamento distribuído de itinerário e pela compensação
        da janela rara em que um participante presumiu aborto após commit em
        outros. Idempotente.
        """
        with self.trava_global:
            reserva = next(
                (r for r in self.reservas.values() if r.get("tx") == tx_id), None)
        if reserva is None:
            # Nada efetivado aqui: garante ao menos o aborto do hold, se houver.
            self.abortar_hold(tx_id, motivo="cancelamento")
            return {"tx": tx_id, "status": "inexistente"}
        if reserva["status"] == "confirmada":
            self._desfazer_reserva(reserva, novo_status="cancelada")
            self._salvar()
        return {"tx": tx_id, "status": reserva["status"]}

    def registrar_itinerario(self, registro: dict) -> None:
        """Grava o registro de itinerário do PASSAGEIRO (lado coordenador).

        Este registro é a visão do passageiro (minhas_reservas) e carrega os
        ids globais ("empresa:carona"); a contabilidade de assentos mora nas
        reservas dos participantes, nunca aqui — por isso os ids globais não
        colidem com os locais em minhas_caronas.
        """
        with self.trava_global:
            self.reservas[registro["id"]] = registro
        self._salvar()

    def marcar_itinerario(self, reserva_id: str, novo_status: str) -> None:
        with self.trava_global:
            registro = self.reservas.get(reserva_id)
            if registro is not None and registro["status"] == "confirmada":
                registro["status"] = novo_status
        self._salvar()

    # ------------------------------------------------------------------
    # Consulta para a busca de itinerários
    # ------------------------------------------------------------------
    def snapshot_caronas(self, data: str) -> list[dict]:
        """Cópia consistente das caronas ativas na data, com a disponibilidade
        por trecho. A busca trabalha sobre este snapshot (leitura barata);
        a palavra final sobre disponibilidade é sempre da confirmação, que
        re-verifica tudo sob as travas."""
        data = _validar_data(_exigir_texto(data, "data", 10))
        with self.trava_global:
            itens = list(self.caronas.values())
        resultado = []
        for carona in itens:
            with self.travas_caronas[carona["id"]]:
                if carona["cancelada"] or carona["data"] != data:
                    continue
                resultado.append({
                    "id": carona["id"],
                    "motorista": carona["motorista"],
                    "rota": list(carona["rota"]),
                    "data": carona["data"],
                    "hora": carona["hora"],
                    "precos": list(carona["precos"]),
                    "disponiveis": [carona["assentos"] - o for o in carona["ocupados"]],
                })
        return resultado

    # ------------------------------------------------------------------
    # Visões e persistência
    # ------------------------------------------------------------------
    @staticmethod
    def _visao_carona(carona: dict) -> dict:
        """Representação de uma carona enviada aos clientes (por trecho)."""
        return {
            "id": carona["id"],
            "motorista": carona["motorista"],
            "rota": list(carona["rota"]),
            "data": carona["data"],
            "hora": carona["hora"],
            "assentos": carona["assentos"],
            "cancelada": carona["cancelada"],
            "trechos": [
                {
                    "trecho": f"{carona['rota'][k]} -> {carona['rota'][k + 1]}",
                    "preco": carona["precos"][k],
                    "disponiveis": carona["assentos"] - carona["ocupados"][k],
                }
                for k in range(len(carona["rota"]) - 1)
            ],
        }

    def _salvar(self) -> None:
        """Grava o snapshot do estado em disco de forma ATÔMICA.

        A serialização acontece sob as travas (visão consistente); a escrita
        usa arquivo temporário + os.replace, de modo que uma queda do
        servidor no meio da gravação nunca deixa o arquivo corrompido:
        ou o arquivo antigo permanece, ou o novo aparece completo.
        """
        with self.trava_persistencia:
            with self.trava_global:
                with self._travar_caronas(list(self.caronas)):
                    texto = json.dumps(
                        {
                            "usuarios": self.usuarios,
                            "caronas": self.caronas,
                            "reservas": self.reservas,
                            # Problema 2: holds e desfechos sobrevivem a
                            # reinícios — um participante que caiu no meio de
                            # um 2PC religa com os assentos ainda retidos e
                            # resolve o hold consultando o coordenador.
                            "holds": self.holds,
                            "desfechos": self.desfechos,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
            os.makedirs(os.path.dirname(self.caminho_arquivo) or ".", exist_ok=True)
            temporario = self.caminho_arquivo + ".tmp"
            with open(temporario, "w", encoding="utf-8") as arquivo:
                arquivo.write(texto)
            os.replace(temporario, self.caminho_arquivo)

    def _carregar(self) -> None:
        """Recarrega o estado do disco na inicialização (se existir)."""
        if not os.path.exists(self.caminho_arquivo):
            return
        with open(self.caminho_arquivo, encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
        self.usuarios = dados.get("usuarios", {})
        self.caronas = dados.get("caronas", {})
        self.reservas = dados.get("reservas", {})
        self.holds = dados.get("holds", {})
        self.desfechos = dados.get("desfechos", {})
        self.travas_caronas = {cid: threading.Lock() for cid in self.caronas}
