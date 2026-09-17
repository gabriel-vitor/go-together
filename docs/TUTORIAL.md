# Tutorial de Execução — VAIJUNTO

Guia passo a passo para executar o sistema completo: servidor, cliente
motorista, cliente passageiro e o teste automatizado de concorrência —
localmente, com Docker e no cenário do laboratório (máquinas distintas).

## Pré-requisitos

Escolha **um** dos dois caminhos:

- **Sem Docker:** Python **3.10 ou superior**. Nada mais — o projeto usa
  apenas a biblioteca padrão (não existe `requirements.txt` a instalar).
- **Com Docker:** Docker Engine + Docker Compose (v2).

Todos os comandos abaixo são executados **na raiz do repositório**
(`go-together/`).

---

## 1. Execução local (sem Docker)

### 1.1 Subir o servidor

```bash
python -m servidor.servidor
```

Saída esperada:

```
[servidor] VAIJUNTO ouvindo em 0.0.0.0:5000
```

O servidor cria/atualiza o arquivo `dados/estado.json` (persistência) e
registra no console cada conexão aberta e encerrada. Deixe este terminal
aberto.

Variáveis de ambiente opcionais: `VAIJUNTO_PORTA` (padrão `5000`),
`VAIJUNTO_DADOS` (padrão `dados/estado.json`).

### 1.2 Cliente motorista (novo terminal)

```bash
python -m cliente.motorista
```

Opcionalmente `python -m cliente.motorista HOST PORTA` para apontar para
outro servidor (padrão `localhost 5000`).

### 1.3 Cliente passageiro (outro terminal)

```bash
python -m cliente.passageiro
```

Pode abrir quantos clientes quiser em paralelo — o servidor atende todos
simultaneamente.

---

## 2. Execução com Docker (uma máquina)

### 2.1 Servidor

```bash
docker compose up --build servidor
```

A porta 5000 é publicada no host e o estado é persistido em `./dados`
(volume), sobrevivendo à recriação do contêiner.

### 2.2 Clientes (terminais separados)

```bash
docker compose run --rm motorista
```

```bash
docker compose run --rm passageiro
```

Dentro da rede do Compose, os clientes alcançam o servidor pelo nome de
serviço `servidor` (DNS interno) — nenhuma configuração extra é necessária.

---

## 3. Laboratório: contêineres em máquinas distintas

Cenário exigido pelo problema: servidor em uma máquina, clientes em outras,
comunicando-se pela rede local.

**Na máquina do servidor:**

1. Suba o servidor:

```bash
docker compose up --build servidor
```

2. Descubra o IP da máquina na rede do laboratório:

```bash
ip addr
```

(no Windows, `ipconfig`). Anote o IPv4 da interface da rede local, por
exemplo `10.0.0.42`.

Como o Compose publica a porta com `ports: "5000:5000"`, o Docker faz o
encaminhamento host→contêiner: qualquer máquina da rede alcança o servidor
via `IP_DO_HOST:5000`, sem redes overlay ou configuração adicional.

**Nas máquinas dos clientes** (substitua pelo IP anotado):

```bash
VAIJUNTO_HOST=10.0.0.42 docker compose run --rm passageiro
```

```bash
VAIJUNTO_HOST=10.0.0.42 docker compose run --rm motorista
```

Ou, sem Docker: `python -m cliente.passageiro 10.0.0.42 5000`.

**Se a conexão for recusada:** verifique se o firewall da máquina do
servidor permite entrada na porta TCP 5000 e se as máquinas estão na mesma
rede (teste com `ping IP_DO_SERVIDOR`).

---

## 4. Roteiro de demonstração (fim a fim)

Com o servidor no ar e dois terminais de cliente abertos:

**No cliente motorista** (`python -m cliente.motorista`):

1. Menu de acesso → `2` (Registrar), ex.: usuário `joao`, senha `1234`.
2. `1` (Publicar carona) e preencha:
   - Rota: `Salvador, Feira de Santana, Vitória da Conquista`
   - Data: `2026-09-25` — Hora: `08:00` — Assentos: `3`
   - Preços: `40, 80` (um por trecho) ou um único valor para todos.
3. `2` (Minhas caronas): observe os **trechos** com preço, assentos livres
   e a lista de passageiros (vazia por enquanto).

**No cliente passageiro** (`python -m cliente.passageiro`):

4. Registre-se (ex.: `ana`) e entre.
5. `1` (Buscar itinerários): origem `Salvador`, destino
   `Vitória da Conquista`, data `2026-09-25`. O sistema lista as opções do
   mais barato ao mais caro — inclusive combinações de caronas de
   motoristas diferentes, quando existirem.
6. `2` (Reservar): escolha o número do itinerário. A confirmação é
   **atômica**: todas as pernas ou nenhuma.
7. `3` (Minhas reservas): confira id, status e preço total.

**De volta ao motorista:**

8. `2` (Minhas caronas): `ana` agora aparece nos trechos reservados, e os
   assentos livres diminuíram **apenas nos trechos usados** — um assento
   ocupado de Salvador a Feira continua livre de Feira a Conquista.

**Extras que valem demonstrar:**

- Reserve o mesmo trecho com outros passageiros até esgotar e veja a
  recusa: `assentos esgotados no trecho ...` (o primeiro a confirmar mantém
  a preferência).
- Cancele a reserva no passageiro (`4`) e veja os assentos voltarem.
- Cancele a carona no motorista (`3`): as reservas que a usavam são
  desfeitas por inteiro (status `cancelada_pelo_motorista`).
- Mate um cliente no meio do uso (feche o terminal): o servidor registra a
  desconexão e continua atendendo os demais, sem assentos presos.
- Reinicie o servidor: usuários, caronas e reservas são recarregados de
  `dados/estado.json`.

---

## 5. Teste automatizado de concorrência

Com o servidor no ar (local ou remoto):

```bash
python -m testes.teste_concorrencia
```

Contra outro host: `python -m testes.teste_concorrencia 10.0.0.42 5000`.
Com Docker (na mesma máquina do servidor):

```bash
docker compose run --rm teste
```

O teste cria usuários e caronas próprios (nomes únicos por rodada, então
pode ser repetido à vontade) e imprime, por cenário, as verificações e as
latências (média, p95, máximo). Saída esperada ao final:

```
RESULTADO: todas as verificações passaram
```

Cenários: disputa de 30 passageiros por 5 assentos (sem venda dupla),
atomicidade de itinerário multi-carona, caronas travadas em ordens opostas
(deadlock) e 200 buscas concorrentes (desempenho). Detalhes no cabeçalho de
[testes/teste_concorrencia.py](../testes/teste_concorrencia.py).

---

## 6. Solução de problemas

| Sintoma | Causa provável / solução |
|---|---|
| `não foi possível conectar a HOST:5000` | Servidor não está no ar, host/porta errados, ou firewall bloqueando a porta 5000 |
| `Address already in use` ao subir o servidor | Já existe um servidor na porta 5000: encerre-o ou use `VAIJUNTO_PORTA=5001` |
| Acentos estranhos no terminal Windows | Cosmético (codificação do console); use `chcp 65001` antes de executar, se incomodar |
| Cliente "congelado" após 10 min parado | Timeout de inatividade do servidor (600 s); basta reconectar |
| Quero zerar os dados | Pare o servidor e apague a pasta `dados/` |
