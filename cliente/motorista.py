"""
Cliente MOTORISTA do VAIJUNTO (interface de terminal).

Permite: autenticar-se, publicar uma carona (rota, data, assentos e preço
por trecho), consultar as caronas publicadas com os passageiros confirmados
em cada trecho, e cancelar uma carona.
"""

import sys

from cliente.comum import Conexao, ErroServidor, autenticar, endereco_servidor, entrada, ler_inteiro


def publicar(conexao: Conexao) -> None:
    print("\n--- Publicar carona ---")
    rota_texto = entrada("Rota (cidades separadas por vírgula, na ordem do percurso):\n> ")
    rota = [c.strip() for c in rota_texto.split(",") if c.strip()]
    if len(rota) < 2:
        print("[erro] informe pelo menos duas cidades")
        return
    data = entrada("Data da partida (AAAA-MM-DD): ").strip()
    hora = entrada("Hora da partida (HH:MM): ").strip()
    assentos = ler_inteiro("Assentos livres no veículo: ")

    # Preço por trecho: um valor único aplicado a todos, ou uma lista.
    n_trechos = len(rota) - 1
    print(f"A rota tem {n_trechos} trecho(s):")
    for k in range(n_trechos):
        print(f"  {k + 1}. {rota[k]} -> {rota[k + 1]}")
    precos_texto = entrada(
        f"Preço por trecho ({n_trechos} valores separados por vírgula, ou 1 valor para todos): ")
    try:
        valores = [float(v.strip().replace(",", ".")) for v in precos_texto.split(",") if v.strip()] \
            if "," in precos_texto else [float(precos_texto.strip().replace(",", "."))]
    except ValueError:
        print("[erro] preços inválidos")
        return
    precos = valores * n_trechos if len(valores) == 1 else valores

    carona = conexao.pedir(
        "publicar_carona", rota=rota, data=data, hora=hora, assentos=assentos, precos=precos)
    print(f"\nCarona publicada! id: {carona['id']}")


def listar(conexao: Conexao) -> None:
    caronas = conexao.pedir("minhas_caronas")
    if not caronas:
        print("\nVocê ainda não publicou caronas.")
        return
    for carona in caronas:
        situacao = "CANCELADA" if carona["cancelada"] else "ativa"
        print(f"\n[{carona['id']}] {carona['data']} {carona['hora']} "
              f"({situacao}, {carona['assentos']} assentos)")
        print("  Rota: " + " -> ".join(carona["rota"]))
        for trecho in carona["trechos"]:
            passageiros = ", ".join(trecho["passageiros"]) or "-"
            print(f"    {trecho['trecho']:40s} R$ {trecho['preco']:>7.2f}  "
                  f"livres: {trecho['disponiveis']}  passageiros: {passageiros}")


def cancelar(conexao: Conexao) -> None:
    carona_id = entrada("Id da carona a cancelar: ").strip()
    print("As reservas de passageiros nesta carona serão canceladas por inteiro.")
    if entrada("Confirmar cancelamento? (s/N): ").strip().lower() != "s":
        return
    resultado = conexao.pedir("cancelar_carona", carona_id=carona_id)
    print(f"Carona cancelada ({resultado['reservas_canceladas']} reserva(s) desfeita(s)).")


def principal() -> None:
    host, porta = endereco_servidor()
    try:
        conexao = Conexao(host, porta)
    except OSError as exc:
        print(f"[erro] não foi possível conectar a {host}:{porta} ({exc})")
        sys.exit(1)
    print(f"Conectado ao servidor VAIJUNTO em {host}:{porta}")
    autenticar(conexao)

    acoes = {"1": publicar, "2": listar, "3": cancelar}
    try:
        while True:
            print("\n=== VAIJUNTO - Motorista ===")
            print("1. Publicar carona")
            print("2. Minhas caronas (passageiros por trecho)")
            print("3. Cancelar carona")
            print("0. Sair")
            opcao = entrada("> ").strip()
            if opcao == "0":
                break
            acao = acoes.get(opcao)
            if acao is None:
                continue
            try:
                acao(conexao)
            except ErroServidor as exc:
                print(f"[erro] {exc}")
    except (ConnectionError, OSError) as exc:
        print(f"[erro] conexão com o servidor perdida: {exc}")
    finally:
        conexao.fechar()


if __name__ == "__main__":
    principal()
