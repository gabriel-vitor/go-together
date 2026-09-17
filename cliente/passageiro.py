"""
Cliente PASSAGEIRO do VAIJUNTO (interface de terminal).

Permite: autenticar-se, buscar itinerários entre origem e destino em uma
data (inclusive combinando caronas de motoristas diferentes), confirmar a
reserva atômica de um itinerário, e consultar ou cancelar suas reservas.
"""

import sys

from cliente.comum import Conexao, ErroServidor, autenticar, endereco_servidor


def _mostrar_itinerarios(itinerarios: list[dict]) -> None:
    for n, itinerario in enumerate(itinerarios, start=1):
        print(f"\n{n}. R$ {itinerario['preco_total']:.2f} "
              f"({itinerario['num_pernas']} perna(s))")
        for perna in itinerario["pernas"]:
            print(f"     {perna['origem']} -> {perna['destino']} | "
                  f"{perna['data']} {perna['hora']} | motorista: {perna['motorista']} | "
                  f"R$ {perna['preco']:.2f} | assentos: {perna['assentos_disponiveis']} | "
                  f"carona {perna['carona_id']}")


def buscar(conexao: Conexao, cache: dict) -> None:
    print("\n--- Buscar itinerários ---")
    origem = input("Cidade de origem: ").strip()
    destino = input("Cidade de destino: ").strip()
    data = input("Data (AAAA-MM-DD): ").strip()
    itinerarios = conexao.pedir("buscar_itinerarios", origem=origem, destino=destino, data=data)
    if not itinerarios:
        print("\nNenhum itinerário encontrado para esses critérios.")
        cache.pop("itinerarios", None)
        return
    print(f"\n{len(itinerarios)} itinerário(s) encontrado(s), do mais barato ao mais caro:")
    _mostrar_itinerarios(itinerarios)
    cache["itinerarios"] = itinerarios
    print("\nUse a opção 'Reservar' para confirmar um deles (a disponibilidade "
          "é garantida apenas na confirmação).")


def reservar(conexao: Conexao, cache: dict) -> None:
    itinerarios = cache.get("itinerarios")
    if not itinerarios:
        print("Busque itinerários primeiro.")
        return
    _mostrar_itinerarios(itinerarios)
    escolha = input("\nNúmero do itinerário a reservar (0 para voltar): ").strip()
    if not escolha.isdigit() or not (1 <= int(escolha) <= len(itinerarios)):
        return
    itinerario = itinerarios[int(escolha) - 1]
    # A reserva envia apenas o essencial de cada perna; o servidor
    # re-valida tudo (preço, índices, disponibilidade) sob as travas.
    pernas = [
        {"carona_id": p["carona_id"], "origem": p["origem"], "destino": p["destino"]}
        for p in itinerario["pernas"]
    ]
    reserva = conexao.pedir("reservar", pernas=pernas)
    print(f"\nReserva CONFIRMADA! id: {reserva['id']} | total R$ {reserva['preco_total']:.2f}")


def minhas_reservas(conexao: Conexao) -> None:
    reservas = conexao.pedir("minhas_reservas")
    if not reservas:
        print("\nVocê não possui reservas.")
        return
    for reserva in reservas:
        print(f"\n[{reserva['id']}] {reserva['data']} | {reserva['status']} | "
              f"R$ {reserva['preco_total']:.2f}")
        for perna in reserva["pernas"]:
            print(f"     {perna['origem']} -> {perna['destino']} | {perna['hora']} | "
                  f"motorista: {perna['motorista']} | carona {perna['carona_id']}")


def cancelar(conexao: Conexao) -> None:
    reserva_id = input("Id da reserva a cancelar: ").strip()
    resultado = conexao.pedir("cancelar_reserva", reserva_id=reserva_id)
    print(f"Reserva {resultado['reserva_id']} cancelada; assentos liberados.")


def principal() -> None:
    host, porta = endereco_servidor()
    try:
        conexao = Conexao(host, porta)
    except OSError as exc:
        print(f"[erro] não foi possível conectar a {host}:{porta} ({exc})")
        sys.exit(1)
    print(f"Conectado ao servidor VAIJUNTO em {host}:{porta}")
    autenticar(conexao)

    cache: dict = {}  # última busca, para o fluxo buscar -> reservar
    try:
        while True:
            print("\n=== VAIJUNTO - Passageiro ===")
            print("1. Buscar itinerários")
            print("2. Reservar itinerário da última busca")
            print("3. Minhas reservas")
            print("4. Cancelar reserva")
            print("0. Sair")
            opcao = input("> ").strip()
            if opcao == "0":
                break
            try:
                if opcao == "1":
                    buscar(conexao, cache)
                elif opcao == "2":
                    reservar(conexao, cache)
                elif opcao == "3":
                    minhas_reservas(conexao)
                elif opcao == "4":
                    cancelar(conexao)
            except ErroServidor as exc:
                print(f"[erro] {exc}")
    except (ConnectionError, OSError) as exc:
        print(f"[erro] conexão com o servidor perdida: {exc}")
    finally:
        conexao.fechar()


if __name__ == "__main__":
    principal()
