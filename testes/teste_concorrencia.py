"""
Teste automatizado de CONCORRÊNCIA e DESEMPENHO do servidor VAIJUNTO.

Submete o servidor a múltiplos clientes simultâneos (cada um com sua própria
conexão TCP e seu próprio thread) disputando os mesmos trechos, e verifica:

  Cenário 1 - Disputa por assentos em um único trecho:
      N passageiros tentam, ao mesmo tempo, reservar o mesmo trecho de uma
      carona com S assentos. Exatamente S reservas devem ser confirmadas —
      nem um assento a mais (overselling) nem a menos.

  Cenário 2 - Atomicidade de itinerário multi-carona:
      Carona A tem assentos de sobra, carona B tem apenas 1. N passageiros
      disputam o itinerário A+B. Só 1 pode confirmar, e a ocupação final de
      A deve ser exatamente 1: se fosse maior, alguma reserva teria sido
      confirmada "pela metade" (garantiu A e perdeu B).

  Cenário 3 - Ausência de deadlock:
      Passageiros reservam itinerários que atravessam as MESMAS caronas em
      ordens de viagem opostas (A depois B vs. B depois A), o pior caso para
      bloqueio mútuo. O teste falha por timeout se o servidor travar.

  Em todos os cenários mede-se o tempo de resposta (média, p95, máximo) das
  operações sob carga.

Uso: python -m testes.teste_concorrencia [host] [porta]
     (padrão: localhost 5000, ou variáveis VAIJUNTO_HOST / VAIJUNTO_PORTA)

O teste cria seus próprios usuários e caronas com nomes únicos por execução,
então pode ser executado repetidas vezes contra o mesmo servidor.
"""

import os
import statistics
import sys
import threading
import time
import uuid

from cliente.comum import Conexao, ErroServidor

HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VAIJUNTO_HOST", "localhost")
PORTA = int(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("VAIJUNTO_PORTA", "5000"))
DATA = "2026-12-01"

# Prefixo único por execução: evita colisão de usuários/cidades entre rodadas.
RODADA = uuid.uuid4().hex[:6]

falhas: list[str] = []


def checar(condicao: bool, descricao: str) -> None:
    status = "OK   " if condicao else "FALHA"
    print(f"  [{status}] {descricao}")
    if not condicao:
        falhas.append(descricao)


def cidade(nome: str) -> str:
    return f"{nome}-{RODADA}"


def nova_conexao_logada(usuario: str) -> Conexao:
    conexao = Conexao(HOST, PORTA)
    try:
        conexao.pedir("registrar", usuario=usuario, senha="senha-teste")
    except ErroServidor:
        pass  # usuário já existia (reuso dentro da mesma rodada)
    conexao.pedir("login", usuario=usuario, senha="senha-teste")
    return conexao


def publicar_carona(conexao: Conexao, rota: list[str], assentos: int) -> str:
    carona = conexao.pedir(
        "publicar_carona", rota=rota, data=DATA, hora="08:00",
        assentos=assentos, precos=[50.0] * (len(rota) - 1))
    return carona["id"]


def disparar_passageiros(n: int, fabrica_pernas, prefixo: str):
    """Cria `n` passageiros, cada um em um thread com sua própria conexão,
    todos liberados AO MESMO TEMPO por uma barreira, para maximizar a
    sobreposição real das requisições de reserva.

    Retorna (numero_de_sucessos, latencias_em_segundos, erros_inesperados).
    """
    barreira = threading.Barrier(n)
    sucessos = []
    latencias = []
    erros_inesperados = []
    trava_resultados = threading.Lock()

    def passageiro(indice: int):
        try:
            conexao = nova_conexao_logada(f"{prefixo}-{RODADA}-{indice}")
            pernas = fabrica_pernas(indice)
            barreira.wait()  # todos disparam juntos
            inicio = time.perf_counter()
            try:
                conexao.pedir("reservar", pernas=pernas)
                ok = True
            except ErroServidor:
                ok = False  # esgotado: resultado esperado para os perdedores
            duracao = time.perf_counter() - inicio
            with trava_resultados:
                latencias.append(duracao)
                if ok:
                    sucessos.append(indice)
            conexao.fechar()
        except Exception as exc:  # noqa: BLE001 - qualquer outra falha é erro do teste
            with trava_resultados:
                erros_inesperados.append(f"passageiro {indice}: {exc!r}")

    threads = [threading.Thread(target=passageiro, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)  # 60 s de folga; sobrar thread vivo = travamento
    travados = [t for t in threads if t.is_alive()]
    if travados:
        erros_inesperados.append(f"{len(travados)} thread(s) não terminaram (possível deadlock)")
    return len(sucessos), latencias, erros_inesperados


def relatar_latencias(rotulo: str, latencias: list[float]) -> None:
    if not latencias:
        return
    ordenadas = sorted(latencias)
    p95 = ordenadas[max(0, int(len(ordenadas) * 0.95) - 1)]
    print(f"  [tempo] {rotulo}: n={len(latencias)}  "
          f"média={statistics.mean(latencias) * 1000:.1f}ms  "
          f"p95={p95 * 1000:.1f}ms  máx={max(latencias) * 1000:.1f}ms")


def ocupacao_no_trecho(conexao_motorista: Conexao, carona_id: str, indice_trecho: int) -> int:
    """Ocupação real de um trecho, consultada pela visão do motorista."""
    for carona in conexao_motorista.pedir("minhas_caronas"):
        if carona["id"] == carona_id:
            trecho = carona["trechos"][indice_trecho]
            return carona["assentos"] - trecho["disponiveis"]
    raise AssertionError("carona não encontrada na visão do motorista")


# ---------------------------------------------------------------------------
def cenario_1() -> None:
    print("\nCenário 1: disputa pelo mesmo trecho (30 passageiros x 5 assentos)")
    motorista = nova_conexao_logada(f"mot1-{RODADA}")
    carona_id = publicar_carona(motorista, [cidade("Salvador"), cidade("Feira")], assentos=5)

    def pernas(_i):
        return [{"carona_id": carona_id, "origem": cidade("Salvador"), "destino": cidade("Feira")}]

    sucessos, latencias, erros = disparar_passageiros(30, pernas, "pas1")
    relatar_latencias("reservar", latencias)
    checar(not erros, f"nenhum erro inesperado {erros or ''}")
    checar(sucessos == 5, f"exatamente 5 reservas confirmadas (obtido: {sucessos})")
    ocupacao = ocupacao_no_trecho(motorista, carona_id, 0)
    checar(ocupacao == 5, f"ocupação do trecho == 5, sem venda dupla (obtido: {ocupacao})")
    motorista.fechar()


def cenario_2() -> None:
    print("\nCenário 2: atomicidade de itinerário multi-carona (20 passageiros x 1 vaga no gargalo)")
    motorista = nova_conexao_logada(f"mot2-{RODADA}")
    # A tem 20 assentos (nunca é gargalo); B tem só 1.
    carona_a = publicar_carona(motorista, [cidade("X"), cidade("Y")], assentos=20)
    carona_b = publicar_carona(motorista, [cidade("Y"), cidade("Z")], assentos=1)

    def pernas(_i):
        return [
            {"carona_id": carona_a, "origem": cidade("X"), "destino": cidade("Y")},
            {"carona_id": carona_b, "origem": cidade("Y"), "destino": cidade("Z")},
        ]

    sucessos, latencias, erros = disparar_passageiros(20, pernas, "pas2")
    relatar_latencias("reservar", latencias)
    checar(not erros, f"nenhum erro inesperado {erros or ''}")
    checar(sucessos == 1, f"exatamente 1 itinerário confirmado (obtido: {sucessos})")
    ocupacao_a = ocupacao_no_trecho(motorista, carona_a, 0)
    checar(ocupacao_a == 1,
           f"carona A ocupada só pelo vencedor — nenhuma reserva pela metade (obtido: {ocupacao_a})")
    motorista.fechar()


def cenario_3() -> None:
    print("\nCenário 3: caronas disputadas em ordens opostas (teste de deadlock, 40 passageiros)")
    motorista = nova_conexao_logada(f"mot3-{RODADA}")
    # Duas caronas em sentidos opostos entre P e R, passando por Q: metade dos
    # passageiros reserva (ida em A, volta em B invertendo cidades) e a outra
    # metade o oposto, forçando aquisição das MESMAS travas em ordens de
    # viagem diferentes.
    carona_a = publicar_carona(motorista, [cidade("P"), cidade("Q"), cidade("R")], assentos=15)
    carona_b = publicar_carona(motorista, [cidade("R"), cidade("Q"), cidade("P")], assentos=15)

    def pernas(indice):
        if indice % 2 == 0:  # P -> Q na carona A, depois Q -> P na carona B
            return [
                {"carona_id": carona_a, "origem": cidade("P"), "destino": cidade("Q")},
                {"carona_id": carona_b, "origem": cidade("Q"), "destino": cidade("P")},
            ]
        return [  # R -> Q na carona B, depois Q -> R na carona A
            {"carona_id": carona_b, "origem": cidade("R"), "destino": cidade("Q")},
            {"carona_id": carona_a, "origem": cidade("Q"), "destino": cidade("R")},
        ]

    sucessos, latencias, erros = disparar_passageiros(40, pernas, "pas3")
    relatar_latencias("reservar", latencias)
    checar(not erros, f"todas as requisições terminaram — sem deadlock {erros or ''}")
    # Cada itinerário par usa trecho 0 de A; cada ímpar usa trecho 1 de A.
    # Com 15 assentos e 20 disputantes de cada lado, esperam-se 15 + 15.
    checar(sucessos == 30, f"30 itinerários confirmados, 15 por sentido (obtido: {sucessos})")
    for carona_id, rotulo in ((carona_a, "A"), (carona_b, "B")):
        for k in range(2):
            ocupacao = ocupacao_no_trecho(motorista, carona_id, k)
            checar(ocupacao <= 15, f"carona {rotulo} trecho {k}: ocupação {ocupacao} <= 15 assentos")
    motorista.fechar()


def cenario_4() -> None:
    print("\nCenário 4: tempo de resposta da busca sob carga (20 clientes x 10 buscas)")
    motorista = nova_conexao_logada(f"mot4-{RODADA}")
    publicar_carona(
        motorista,
        [cidade("Salvador"), cidade("Feira"), cidade("Itaberaba"), cidade("Conquista")],
        assentos=4)
    latencias: list[float] = []
    erros: list[str] = []
    trava = threading.Lock()
    barreira = threading.Barrier(20)

    def consultante(indice: int):
        try:
            conexao = nova_conexao_logada(f"pas4-{RODADA}-{indice}")
            barreira.wait()
            for _ in range(10):
                inicio = time.perf_counter()
                conexao.pedir("buscar_itinerarios",
                              origem=cidade("Salvador"), destino=cidade("Conquista"), data=DATA)
                with trava:
                    latencias.append(time.perf_counter() - inicio)
            conexao.fechar()
        except Exception as exc:  # noqa: BLE001
            with trava:
                erros.append(repr(exc))

    threads = [threading.Thread(target=consultante, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    relatar_latencias("buscar_itinerarios", latencias)
    checar(not erros, f"200 buscas concluídas sem erro {erros or ''}")
    motorista.fechar()


def principal() -> None:
    print(f"Teste de concorrência do VAIJUNTO — servidor {HOST}:{PORTA} (rodada {RODADA})")
    inicio = time.perf_counter()
    cenario_1()
    cenario_2()
    cenario_3()
    cenario_4()
    duracao = time.perf_counter() - inicio
    print(f"\nDuração total: {duracao:.1f}s")
    if falhas:
        print(f"RESULTADO: {len(falhas)} verificação(ões) FALHARAM")
        sys.exit(1)
    print("RESULTADO: todas as verificações passaram")


if __name__ == "__main__":
    principal()
