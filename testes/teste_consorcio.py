"""
Teste automatizado do CONSÓRCIO (Problema 2): carga, concorrência ENTRE
SERVIDORES e injeção de falhas.

Pré-requisito: as três empresas no ar (docker compose up -d vaijunto
expresso rotasul, ou três processos locais). O teste conecta a cada empresa
pelo protocolo TCP de cliente, como um passageiro/motorista real.

Cenários:
  1. Atomicidade distribuída sob disputa: itinerário com etapas nas TRÊS
     empresas e gargalo de 1 assento; 12 passageiros (4 por empresa)
     disputam -> exatamente 1 confirma, e as caronas folgadas terminam com
     ocupação 1 (valor maior = itinerário confirmado pela metade).
  2. Exclusão mútua inter-empresas: 18 passageiros, vindos das três
     empresas, disputam 3 assentos de um trecho de UMA empresa -> exatamente
     3 confirmam (nenhum assento vendido duas vezes entre servidores).
  3. Deadlock distribuído: itinerários que atravessam as MESMAS duas
     empresas em ordens opostas (ida A->B e volta B->A) -> todos terminam
     (wait-die + ordem canônica), sem exceder a capacidade.
  4. (--falhas) Injeção de falha: derruba uma empresa NO MEIO da carga
     (docker compose stop), verifica que as demais continuam operando e
     que reservas envolvendo a empresa caída falham com erro limpo; religa
     (docker compose start) e verifica a consistência global: em TODA
     empresa, a ocupação de cada trecho é exatamente o número de
     passageiros confirmados nele (nenhum assento preso por hold órfão) e
     nenhum itinerário ficou pela metade.

Uso:
  python -m testes.teste_consorcio \
      --empresa vaijunto=localhost:5000 --empresa expresso=localhost:5001 \
      --empresa rotasul=localhost:5002 [--falhas] [--servico-falha rotasul]
"""

import argparse
import subprocess
import sys
import threading
import time
import uuid

from cliente.comum import Conexao, ErroServidor

RODADA = uuid.uuid4().hex[:6]
DATA = "2026-12-20"
falhas_do_teste: list[str] = []

EMPRESAS: dict[str, tuple[str, int]] = {}  # nome -> (host, porta TCP)
LEASE = 30  # manter igual ao VAIJUNTO_LEASE dos servidores


def checar(condicao: bool, descricao: str) -> None:
    print(f"  [{'OK   ' if condicao else 'FALHA'}] {descricao}")
    if not condicao:
        falhas_do_teste.append(descricao)


def cid(nome: str) -> str:
    return f"{nome}-{RODADA}"


def conectar(empresa: str, usuario: str) -> Conexao:
    conexao = Conexao(*EMPRESAS[empresa])
    try:
        conexao.pedir("registrar", usuario=usuario, senha="teste")
    except ErroServidor:
        pass
    conexao.pedir("login", usuario=usuario, senha="teste")
    return conexao


def publicar(empresa: str, motorista: str, rota: list[str], assentos: int) -> str:
    conexao = conectar(empresa, motorista)
    carona = conexao.pedir("publicar_carona", rota=rota, data=DATA, hora="08:00",
                           assentos=assentos, precos=[50.0] * (len(rota) - 1))
    conexao.fechar()
    return f"{empresa}:{carona['id']}"


def ocupacao(empresa: str, motorista: str, carona_global: str) -> list[dict]:
    """Visão por trecho {ocupados, passageiros} da carona, pelo dono."""
    conexao = conectar(empresa, motorista)
    local = carona_global.split(":", 1)[1]
    for carona in conexao.pedir("minhas_caronas"):
        if carona["id"] == local:
            conexao.fechar()
            return [{"ocupados": carona["assentos"] - t["disponiveis"],
                     "passageiros": t["passageiros"]} for t in carona["trechos"]]
    conexao.fechar()
    raise AssertionError(f"carona {carona_global} não encontrada")


def disputar(n_por_empresa: int, etapas, prefixo: str):
    """n passageiros POR empresa, cada um com conexão própria, liberados
    juntos por barreira, todos tentando reservar as mesmas etapas."""
    nomes = list(EMPRESAS)
    total = n_por_empresa * len(nomes)
    barreira = threading.Barrier(total)
    sucessos, erros = [], []
    trava = threading.Lock()

    def passageiro(empresa: str, indice: int):
        usuario = f"{prefixo}-{RODADA}-{empresa}-{indice}"
        try:
            conexao = conectar(empresa, usuario)
            barreira.wait()
            try:
                reserva = conexao.pedir("reservar", etapas=etapas)
                with trava:
                    sucessos.append((usuario, empresa, reserva["id"]))
            except ErroServidor:
                pass  # perder a disputa é desfecho esperado
            conexao.fechar()
        except Exception as exc:  # noqa: BLE001
            with trava:
                erros.append(f"{usuario}: {exc!r}")

    threads = [threading.Thread(target=passageiro, args=(e, i))
               for e in nomes for i in range(n_por_empresa)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    if any(t.is_alive() for t in threads):
        erros.append("threads não terminaram (possível deadlock distribuído)")
    return sucessos, erros


# ---------------------------------------------------------------------------
def cenario_1():
    print("\nCenário 1: atomicidade distribuída (3 empresas, gargalo de 1 assento)")
    nomes = list(EMPRESAS)
    mot = f"mot1-{RODADA}"
    c1 = publicar(nomes[0], mot, [cid("A"), cid("B")], assentos=4)
    c2 = publicar(nomes[1], mot, [cid("B"), cid("C")], assentos=1)  # gargalo
    c3 = publicar(nomes[2], mot, [cid("C"), cid("D")], assentos=4)
    etapas = [{"carona_id": c1, "origem": cid("A"), "destino": cid("B")},
              {"carona_id": c2, "origem": cid("B"), "destino": cid("C")},
              {"carona_id": c3, "origem": cid("C"), "destino": cid("D")}]

    sucessos, erros = disputar(4, etapas, "p1")
    checar(not erros, f"sem erros inesperados {erros or ''}")
    checar(len(sucessos) == 1, f"exatamente 1 itinerário confirmado (obtido: {len(sucessos)})")
    time.sleep(2)  # aborts em trânsito
    oc1 = ocupacao(nomes[0], mot, c1)[0]["ocupados"]
    oc3 = ocupacao(nomes[2], mot, c3)[0]["ocupados"]
    checar(oc1 == 1, f"{nomes[0]} ocupada só pelo vencedor — nada pela metade (obtido: {oc1})")
    checar(oc3 == 1, f"{nomes[2]} ocupada só pelo vencedor — nada pela metade (obtido: {oc3})")


def cenario_2():
    print("\nCenário 2: exclusão mútua entre servidores (18 passageiros x 3 assentos)")
    nomes = list(EMPRESAS)
    mot = f"mot2-{RODADA}"
    carona = publicar(nomes[1], mot, [cid("E"), cid("F")], assentos=3)
    etapas = [{"carona_id": carona, "origem": cid("E"), "destino": cid("F")}]

    sucessos, erros = disputar(6, etapas, "p2")
    checar(not erros, f"sem erros inesperados {erros or ''}")
    checar(len(sucessos) == 3, f"exatamente 3 confirmadas (obtido: {len(sucessos)})")
    time.sleep(2)
    visao = ocupacao(nomes[1], mot, carona)[0]
    checar(visao["ocupados"] == 3,
           f"ocupação do trecho == 3, sem venda dupla (obtido: {visao['ocupados']})")


def cenario_3():
    print("\nCenário 3: deadlock distribuído (ordens opostas entre 2 empresas)")
    nomes = list(EMPRESAS)
    mot = f"mot3-{RODADA}"
    ida = publicar(nomes[0], mot, [cid("P"), cid("Q")], assentos=10)
    volta = publicar(nomes[1], mot, [cid("Q"), cid("P")], assentos=10)
    it_ab = [{"carona_id": ida, "origem": cid("P"), "destino": cid("Q")},
             {"carona_id": volta, "origem": cid("Q"), "destino": cid("P")}]
    it_ba = [{"carona_id": volta, "origem": cid("Q"), "destino": cid("P")},
             {"carona_id": ida, "origem": cid("P"), "destino": cid("Q")}]

    # Metade disputa na ordem A->B, metade na ordem B->A, simultaneamente.
    resultados = {}

    def rodar(nome, etapas):
        resultados[nome] = disputar(4, etapas, f"p3{nome}")

    t1 = threading.Thread(target=rodar, args=("ab", it_ab))
    t2 = threading.Thread(target=rodar, args=("ba", it_ba))
    t1.start(); t2.start(); t1.join(timeout=180); t2.join(timeout=180)

    erros = resultados["ab"][1] + resultados["ba"][1]
    total = len(resultados["ab"][0]) + len(resultados["ba"][0])
    checar(not erros, f"todas as requisições terminaram — sem deadlock {erros or ''}")
    checar(total == 10, f"10 confirmadas (capacidade compartilhada; obtido: {total})")
    time.sleep(2)
    for carona, dono in ((ida, nomes[0]), (volta, nomes[1])):
        oc = ocupacao(dono, mot, carona)[0]["ocupados"]
        checar(oc == 10, f"{dono}: ocupação {oc} == 10 (esgotado sem exceder)")


def cenario_4(servico_falha: str, pasta_compose: str):
    print(f"\nCenário 4: injeção de falha (derrubando '{servico_falha}' sob carga)")
    nomes = list(EMPRESAS)
    sobreviventes = [n for n in nomes if n != servico_falha]
    mot = f"mot4-{RODADA}"
    c_sobrev = publicar(sobreviventes[0], mot, [cid("G"), cid("H")], assentos=50)
    c_falho = publicar(servico_falha, mot, [cid("H"), cid("I")], assentos=50)
    # Carona exclusiva para a SONDA de vida durante a falha (a carga pode
    # esgotar c_sobrev; a sonda não pode ser confundida por isso).
    c_sonda = publicar(sobreviventes[0], mot, [cid("J"), cid("K")], assentos=50)
    it_misto = [{"carona_id": c_sobrev, "origem": cid("G"), "destino": cid("H")},
                {"carona_id": c_falho, "origem": cid("H"), "destino": cid("I")}]
    it_sobrev = [{"carona_id": c_sonda, "origem": cid("J"), "destino": cid("K")}]

    def compose(*argumentos):
        subprocess.run(["docker", "compose", *argumentos], cwd=pasta_compose,
                       check=True, capture_output=True)

    # Carga contínua de reservas mistas enquanto a falha é injetada.
    parar = threading.Event()
    confirmadas_mistas, recusadas_mistas, travadas = [], [], []

    def carga(indice):
        conexao = conectar(sobreviventes[0], f"p4-{RODADA}-{indice}")
        n = 0
        while not parar.is_set():
            n += 1
            inicio = time.time()
            try:
                r = conexao.pedir("reservar", etapas=it_misto)
                confirmadas_mistas.append(r["id"])
            except ErroServidor:
                recusadas_mistas.append(time.time() - inicio)
            except (ConnectionError, OSError):
                break
            if time.time() - inicio > 15:
                travadas.append(n)
            time.sleep(0.3)
        conexao.fechar()

    threads = [threading.Thread(target=carga, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()

    time.sleep(3)
    print(f"  [ação] docker compose stop {servico_falha}")
    compose("stop", servico_falha)
    time.sleep(8)  # carga continua batendo na empresa caída

    # Com a empresa caída, as DEMAIS precisam continuar operando.
    try:
        conexao = conectar(sobreviventes[0], f"p4b-{RODADA}")
        reserva = conexao.pedir("reservar", etapas=it_sobrev)
        conexao.fechar()
        checar(True, "empresas sobreviventes seguem confirmando reservas locais")
    except Exception as exc:  # noqa: BLE001
        checar(False, f"reserva local durante a falha: {exc!r}")

    parar.set()
    for t in threads:
        t.join(timeout=60)
    checar(not travadas, "nenhuma requisição travou aguardando a empresa caída (timeouts agiram)")
    checar(len(recusadas_mistas) > 0,
           f"reservas com a empresa caída falharam com erro limpo ({len(recusadas_mistas)}x)")

    print(f"  [ação] docker compose start {servico_falha}")
    compose("start", servico_falha)
    # Espera a empresa voltar e o varredor resolver holds (lease + folga).
    prazo = time.time() + 90
    while time.time() < prazo:
        try:
            Conexao(*EMPRESAS[servico_falha]).fechar()
            break
        except OSError:
            time.sleep(2)
    print(f"  [ação] aguardando janela de lease ({LEASE}s + folga) para consistência...")
    time.sleep(LEASE + 15)

    # ---- Consistência global após a recuperação ----
    # (a) nenhum assento preso: ocupação == passageiros confirmados por trecho
    coerentes = True
    for carona, dono in ((c_sobrev, sobreviventes[0]), (c_falho, servico_falha)):
        for i, trecho in enumerate(ocupacao(dono, mot, carona)):
            if trecho["ocupados"] != len(trecho["passageiros"]):
                coerentes = False
                checar(False, f"{dono} trecho {i}: ocupação {trecho['ocupados']} != "
                              f"{len(trecho['passageiros'])} confirmados (assento preso?)")
    if coerentes:
        checar(True, "ocupação == confirmados em todos os trechos (nenhum assento preso)")

    # (b) nenhum itinerário pela metade: cada confirmação mista ocupa UMA
    # presença em cada empresa — a CONTAGEM por usuário deve ser idêntica
    # nos dois lados (um usuário da carga confirma várias vezes, então a
    # comparação é por multiplicidade, não por conjunto).
    from collections import Counter
    pres_sobrev = Counter(p for p in ocupacao(sobreviventes[0], mot, c_sobrev)[0]["passageiros"]
                          if p.startswith(f"p4-{RODADA}"))
    pres_falho = Counter(p for p in ocupacao(servico_falha, mot, c_falho)[0]["passageiros"]
                         if p.startswith(f"p4-{RODADA}"))
    pela_metade = {u for u in (set(pres_sobrev) | set(pres_falho))
                   if pres_sobrev[u] != pres_falho[u]}
    checar(not pela_metade,
           f"nenhum itinerário confirmado pela metade {sorted(pela_metade) or ''}")
    checar(sum(pres_falho.values()) == len(confirmadas_mistas),
           f"confirmações no cliente ({len(confirmadas_mistas)}) == presenças no dono "
           f"({sum(pres_falho.values())})")


def principal():
    parser = argparse.ArgumentParser()
    parser.add_argument("--empresa", action="append", required=True,
                        help="nome=host:porta (TCP), uma por empresa")
    parser.add_argument("--falhas", action="store_true",
                        help="habilita o cenário 4 (requer docker compose)")
    parser.add_argument("--servico-falha", default="rotasul")
    parser.add_argument("--pasta-compose", default=".")
    args = parser.parse_args()

    for item in args.empresa:
        nome, _, endereco = item.partition("=")
        host, _, porta = endereco.partition(":")
        EMPRESAS[nome] = (host, int(porta or 5000))
    if len(EMPRESAS) < 3:
        print("são necessárias pelo menos 3 empresas")
        sys.exit(2)

    print(f"Teste do consórcio — rodada {RODADA} — empresas: {', '.join(EMPRESAS)}")
    inicio = time.perf_counter()
    cenario_1()
    cenario_2()
    cenario_3()
    if args.falhas:
        cenario_4(args.servico_falha, args.pasta_compose)
    else:
        print("\n(cenário 4 de injeção de falhas pulado; use --falhas)")

    print(f"\nDuração: {time.perf_counter() - inicio:.1f}s")
    if falhas_do_teste:
        print(f"RESULTADO: {len(falhas_do_teste)} verificação(ões) FALHARAM")
        sys.exit(1)
    print("RESULTADO: todas as verificações passaram")


if __name__ == "__main__":
    principal()
