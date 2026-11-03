"""
Simulador de passageiros do consórcio VAIJUNTO (Problema 2).

Gera, de forma autônoma e sem interface gráfica, um grande volume de buscas
e reservas SIMULTÂNEAS a partir dos servidores de TODAS as empresas, para
avaliar o comportamento do sistema sob carga.

Funcionamento:
  1. semeia motoristas e caronas aleatórias em cada empresa (rotas sorteadas
     de um conjunto de cidades compartilhado — é o que cria itinerários que
     cruzam empresas);
  2. dispara N passageiros por empresa, cada um em um thread com sua própria
     conexão TCP, repetindo o ciclo buscar -> reservar -> (às vezes) cancelar
     durante o tempo configurado;
  3. imprime as métricas: vazão, sucessos, conflitos (assento esgotado — um
     resultado ESPERADO sob disputa, não um erro) e latências.

Uso:
  python -m simulador.simulador --tcp host1:5000 host2:5001 host3:5002 \
      [--passageiros 5] [--duracao 30] [--semente 42]
"""

import argparse
import random
import statistics
import threading
import time
import uuid

from cliente.comum import Conexao, ErroServidor

CIDADES = ["Salvador", "Feira de Santana", "Itaberaba", "Vitoria da Conquista",
           "Jequie", "Ilheus", "Montes Claros"]
DATA = "2026-12-15"


class Metricas:
    def __init__(self):
        self.trava = threading.Lock()
        self.buscas = 0
        self.reservas_ok = 0
        self.conflitos = 0
        self.cancelamentos = 0
        self.erros = 0
        self.lat_busca: list[float] = []
        self.lat_reserva: list[float] = []

    def registrar(self, campo, latencia=None, lista=None):
        with self.trava:
            setattr(self, campo, getattr(self, campo) + 1)
            if lista is not None and latencia is not None:
                getattr(self, lista).append(latencia)


def conectar_logado(endereco: tuple[str, int], usuario: str) -> Conexao:
    conexao = Conexao(*endereco)
    try:
        conexao.pedir("registrar", usuario=usuario, senha="simulador")
    except ErroServidor:
        pass
    conexao.pedir("login", usuario=usuario, senha="simulador")
    return conexao


def semear_caronas(enderecos, rodada, caronas_por_empresa, rng):
    """Publica caronas aleatórias em cada empresa (motoristas próprios)."""
    total = 0
    for indice, endereco in enumerate(enderecos):
        conexao = conectar_logado(endereco, f"sim-mot-{rodada}-{indice}")
        for _ in range(caronas_por_empresa):
            inicio = rng.randrange(0, len(CIDADES) - 1)
            fim = rng.randrange(inicio + 1, min(inicio + 4, len(CIDADES)))
            rota = CIDADES[inicio:fim + 1]
            try:
                conexao.pedir("publicar_carona", rota=rota, data=DATA,
                              hora=f"{rng.randrange(5, 22):02d}:00",
                              assentos=rng.randrange(1, 5),
                              precos=[float(rng.randrange(20, 90))] * (len(rota) - 1))
                total += 1
            except ErroServidor:
                pass
        conexao.fechar()
    return total


def passageiro(endereco, usuario, fim, metricas, rng):
    try:
        conexao = conectar_logado(endereco, usuario)
    except OSError:
        metricas.registrar("erros")
        return
    minhas: list[str] = []
    while time.time() < fim:
        try:
            origem, destino = rng.sample(CIDADES, 2)
            inicio = time.perf_counter()
            resposta = conexao.pedir("buscar_itinerarios", origem=origem,
                                     destino=destino, data=DATA)
            metricas.registrar("buscas", time.perf_counter() - inicio, "lat_busca")
            itinerarios = resposta.get("itinerarios", []) \
                if isinstance(resposta, dict) else resposta
            if itinerarios and rng.random() < 0.7:
                alvo = rng.choice(itinerarios)
                etapas = [{"carona_id": e["carona_id"], "origem": e["origem"],
                           "destino": e["destino"]} for e in alvo["etapas"]]
                inicio = time.perf_counter()
                try:
                    reserva = conexao.pedir("reservar", etapas=etapas)
                    metricas.registrar("reservas_ok",
                                       time.perf_counter() - inicio, "lat_reserva")
                    minhas.append(reserva["id"])
                except ErroServidor:
                    # Assento esgotado / empresa indisponível: perder a
                    # disputa é um desfecho correto do sistema sob carga.
                    metricas.registrar("conflitos",
                                       time.perf_counter() - inicio, "lat_reserva")
            if minhas and rng.random() < 0.15:
                try:
                    conexao.pedir("cancelar_reserva", reserva_id=minhas.pop())
                    metricas.registrar("cancelamentos")
                except ErroServidor:
                    pass
        except (ConnectionError, OSError):
            metricas.registrar("erros")
            break
        except ErroServidor:
            metricas.registrar("erros")
    conexao.fechar()


def resumo(nome, valores):
    if not valores:
        return f"  {nome}: sem amostras"
    ordenados = sorted(valores)
    p95 = ordenados[max(0, int(len(ordenados) * 0.95) - 1)]
    return (f"  {nome}: n={len(valores)}  média={statistics.mean(valores)*1000:.1f}ms  "
            f"p95={p95*1000:.1f}ms  máx={max(valores)*1000:.1f}ms")


def principal():
    parser = argparse.ArgumentParser(description="Simulador de passageiros do consórcio")
    parser.add_argument("--tcp", nargs="+", required=True,
                        help="endereços TCP das empresas (host:porta ...)")
    parser.add_argument("--passageiros", type=int, default=5,
                        help="passageiros simultâneos POR empresa")
    parser.add_argument("--duracao", type=int, default=30, help="segundos de carga")
    parser.add_argument("--caronas", type=int, default=8, help="caronas semeadas por empresa")
    parser.add_argument("--semente", type=int, default=None)
    args = parser.parse_args()

    enderecos = []
    for item in args.tcp:
        host, _, porta = item.partition(":")
        enderecos.append((host, int(porta or 5000)))

    rodada = uuid.uuid4().hex[:6]
    rng = random.Random(args.semente)
    print(f"[simulador] rodada {rodada} | {len(enderecos)} empresas | "
          f"{args.passageiros} passageiros/empresa | {args.duracao}s")

    publicadas = semear_caronas(enderecos, rodada, args.caronas, rng)
    print(f"[simulador] {publicadas} caronas semeadas")

    metricas = Metricas()
    fim = time.time() + args.duracao
    threads = []
    for indice_empresa, endereco in enumerate(enderecos):
        for n in range(args.passageiros):
            usuario = f"sim-pas-{rodada}-{indice_empresa}-{n}"
            t = threading.Thread(
                target=passageiro,
                args=(endereco, usuario, fim, metricas,
                      random.Random((args.semente or 0) * 1000 + indice_empresa * 100 + n)))
            threads.append(t)
            t.start()
    for t in threads:
        t.join()

    duracao = args.duracao
    total_ops = metricas.buscas + metricas.reservas_ok + metricas.conflitos
    print("\n===== RESULTADO DA SIMULAÇÃO =====")
    print(f"  buscas: {metricas.buscas}  |  reservas confirmadas: {metricas.reservas_ok}  |  "
          f"conflitos (esgotado): {metricas.conflitos}")
    print(f"  cancelamentos: {metricas.cancelamentos}  |  erros de conexão: {metricas.erros}")
    print(f"  vazão: {total_ops / duracao:.1f} operações/s")
    print(resumo("busca distribuída", metricas.lat_busca))
    print(resumo("reserva (2PC)", metricas.lat_reserva))


if __name__ == "__main__":
    principal()
