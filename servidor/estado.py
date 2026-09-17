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
import uuid
from contextlib import contextmanager
from datetime import datetime

# Limites de sanidade nas entradas (defesa contra clientes defeituosos).
MAX_CIDADES_ROTA = 20
MAX_ASSENTOS = 50
MAX_ETAPAS_RESERVA = 4


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

        self._salvar()
        return {"carona_id": carona_id, "reservas_canceladas": len(afetadas)}

    # ------------------------------------------------------------------
    # Reservas (lado passageiro)
    # ------------------------------------------------------------------
    def reservar(self, passageiro, etapas) -> dict:
        """Confirma atomicamente um itinerário com uma ou mais etapas.

        Fase 1 (sem travas de carona): valida a estrutura do pedido, resolve
        as caronas e calcula quantos assentos serão necessários em cada
        trecho de cada carona (`demanda`).
        Fase 2 (seção crítica): trava as caronas em ordem global de id,
        re-verifica TODA a disponibilidade e só então grava. Qualquer falha
        aborta antes da primeira escrita — tudo ou nada.
        """
        if not isinstance(etapas, list) or not (1 <= len(etapas) <= MAX_ETAPAS_RESERVA):
            raise ErroNegocio(f"etapas deve ser uma lista de 1 a {MAX_ETAPAS_RESERVA} itens")

        # --- Fase 1: resolução e validação ---
        with self.trava_global:
            caronas = {}
            for p in etapas:
                if not isinstance(p, dict):
                    raise ErroNegocio("cada etapa deve ser um objeto")
                cid = _exigir_texto(p.get("carona_id"), "carona_id", 40)
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

        # O itinerário deve ser contíguo: o destino de uma etapa é a origem
        # da seguinte, e todas as etapas na mesma data.
        for anterior, seguinte in zip(etapas_resolvidas, etapas_resolvidas[1:]):
            if anterior["destino"].casefold() != seguinte["origem"].casefold():
                raise ErroNegocio(
                    "itinerário descontínuo: o destino de uma etapa deve ser a origem da próxima")
        if len(datas) != 1:
            raise ErroNegocio("todas as etapas do itinerário devem ser na mesma data")

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
        self.travas_caronas = {cid: threading.Lock() for cid in self.caronas}
