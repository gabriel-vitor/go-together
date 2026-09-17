"""
Busca de itinerários do VAIJUNTO.

O problema é modelado como busca de caminhos em um grafo implícito:
  * nós  = cidades;
  * arestas = "etapas" possíveis, isto é, qualquer sub-percurso contíguo
    (cidade i -> cidade j, i < j) de uma carona ativa na data, com pelo
    menos 1 assento disponível em TODOS os trechos intermediários.

Uma busca em profundidade (DFS) parte da origem e encadeia etapas até o
destino, respeitando:
  * no máximo MAX_ETAPAS etapas (evita itinerários absurdos e limita o
    espaço de busca);
  * não repetir cidade já visitada (evita ciclos);
  * não usar a mesma carona duas vezes no itinerário (se a carona serve,
    a etapa direta dela já cobre o percurso maior).

A busca opera sobre um SNAPSHOT do estado: é uma consulta, não uma promessa.
Entre a busca e a confirmação outro passageiro pode tomar o assento; a
confirmação re-verifica tudo sob as travas e responde com erro nesse caso
("o primeiro a confirmar mantém a preferência").

Ordenação dos resultados: menor preço total, depois menos etapas (menos
baldeações), depois horário de partida mais cedo.
"""

MAX_ETAPAS = 3
MAX_RESULTADOS = 10
# Teto de nós expandidos na DFS, para a busca ter custo previsível mesmo
# com muitas caronas cadastradas.
MAX_EXPANSOES = 20000


def buscar(caronas: list[dict], origem: str, destino: str) -> list[dict]:
    """Monta os itinerários possíveis de `origem` a `destino`.

    `caronas` é o snapshot devolvido por Estado.snapshot_caronas(data):
    apenas caronas ativas na data, com `disponiveis` por trecho.
    """
    origem_norm = origem.casefold()
    destino_norm = destino.casefold()
    if origem_norm == destino_norm:
        return []

    resultados: list[dict] = []
    expansoes = [0]  # lista de 1 posição para ser mutável dentro da recursão

    def dfs(cidade_atual: str, etapas: list[dict], visitadas: set[str]):
        if expansoes[0] >= MAX_EXPANSOES:
            return
        for carona in caronas:
            if any(p["carona_id"] == carona["id"] for p in etapas):
                continue  # já usamos esta carona neste itinerário
            rota_norm = [c.casefold() for c in carona["rota"]]
            if cidade_atual not in rota_norm:
                continue
            i = rota_norm.index(cidade_atual)
            # Tenta descer em cada cidade posterior da rota desta carona,
            # enquanto houver assento em todos os trechos do sub-percurso.
            for j in range(i + 1, len(rota_norm)):
                expansoes[0] += 1
                if carona["disponiveis"][j - 1] < 1:
                    break  # trecho j-1 esgotado: percursos mais longos também
                proxima = rota_norm[j]
                if proxima in visitadas:
                    continue
                etapa = {
                    "carona_id": carona["id"],
                    "motorista": carona["motorista"],
                    "origem": carona["rota"][i],
                    "destino": carona["rota"][j],
                    "data": carona["data"],
                    "hora": carona["hora"],
                    "preco": round(sum(carona["precos"][i:j]), 2),
                    "assentos_disponiveis": min(carona["disponiveis"][i:j]),
                }
                if proxima == destino_norm:
                    resultados.append({
                        "etapas": etapas + [etapa],
                        "preco_total": round(sum(p["preco"] for p in etapas) + etapa["preco"], 2),
                        "num_etapas": len(etapas) + 1,
                    })
                elif len(etapas) + 1 < MAX_ETAPAS:
                    dfs(proxima, etapas + [etapa], visitadas | {proxima})

    dfs(origem_norm, [], {origem_norm})

    resultados.sort(key=lambda it: (it["preco_total"], it["num_etapas"], it["etapas"][0]["hora"]))
    return resultados[:MAX_RESULTADOS]
