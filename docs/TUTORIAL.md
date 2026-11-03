# Tutorial de Execução — VAIJUNTO (Consórcio de Caronas)

Guia passo a passo para executar o consórcio completo do **Problema 2**:
as três empresas (`vaijunto`, `expresso`, `rotasul`), os clientes motorista
e passageiro, o teste automatizado com injeção de falhas e o simulador de
carga — em uma máquina, com Docker e no cenário do laboratório (máquinas
distintas).

> O tutorial da versão centralizada (Problema 1) está preservado na tag
> [`problema-1`](../../../tree/problema-1).

## Pré-requisitos

- **Com Docker (recomendado):** Docker Engine + Docker Compose (v2).
- **Sem Docker:** Python **3.10+** e `pip install -r requirements.txt`
  (instala `fastapi`, `uvicorn` e `requests`, permitidos pelo enunciado
  para a API REST; toda a coordenação distribuída é implementação própria).

Todos os comandos abaixo são executados **na raiz do repositório**
(`go-together/`).

---

## 1. Subir o consórcio (Docker, uma máquina)

```bash
docker compose up --build -d vaijunto expresso rotasul
```

Sobe as três empresas, cada uma com o seu próprio volume de dados em
`./dados/<empresa>`:

| Empresa | TCP (clientes) | REST (entre servidores) |
|---|---|---|
| `vaijunto` | 5000 | 8000 |
| `expresso` | 5001 | 8001 |
| `rotasul` | 5002 | 8002 |

Verifique a saúde (ou abra as URLs no navegador):

```bash
curl http://127.0.0.1:8000/ping
```

Cada empresa responde com o próprio nome e o valor do relógio de Lamport.
`docker compose ps` deve listar os 3 contêineres com os dois pares de
portas cada.

**Sem Docker:** abra três terminais e, em cada um, suba uma empresa
(exemplo em PowerShell; use `127.0.0.1`, e não `localhost` — ver Seção 7):

```bash
$env:EMPRESA="vaijunto"; $env:VAIJUNTO_PORTA="5000"; $env:VAIJUNTO_PORTA_REST="8000"; $env:PARES="expresso=http://127.0.0.1:8001,rotasul=http://127.0.0.1:8002"; python -m servidor.servidor
```

(ajuste `EMPRESA`, portas e `PARES` para as outras duas).

---

## 2. Clientes

Cada cliente conecta **somente ao servidor da sua própria empresa** — a
variável `VAIJUNTO_HOST` escolhe qual:

```bash
docker compose run --rm motorista
```

```bash
docker compose run --rm -e VAIJUNTO_HOST=expresso passageiro
```

Sem Docker: `python -m cliente.passageiro 127.0.0.1 5001` (host e porta da
empresa desejada como argumentos).

---

## 3. Roteiro de demonstração (fim a fim)

O objetivo é reproduzir o cenário do enunciado: um passageiro da
`vaijunto` monta a viagem Salvador → Montes Claros com um trecho em cada
empresa.

**Passo 1 — um motorista em cada empresa.** Grafe as cidades de forma
idêntica nas três (a emenda dos trechos compara o texto exato) e use a
**mesma data**:

| Comando | Usuário | Rota | Hora |
|---|---|---|---|
| `docker compose run --rm motorista` | `carlos` | `Salvador, Feira de Santana` | 08:00 |
| `docker compose run --rm -e VAIJUNTO_HOST=expresso motorista` | `maria` | `Feira de Santana, Vitoria da Conquista` | 12:00 |
| `docker compose run --rm -e VAIJUNTO_HOST=rotasul motorista` | `joao` | `Vitoria da Conquista, Montes Claros` | 17:00 |

Em cada cliente: `2` (Registrar) → `1` (Publicar carona) → preencha rota,
data (ex.: `2026-12-20`), hora, assentos (`2`) e preço → `0` (Sair).

**Passo 2 — busca e reserva distribuídas.** Entre como passageiro da
`vaijunto`:

```bash
docker compose run --rm passageiro
```

Registre-se (ex.: `ana`), busque `Salvador` → `Montes Claros` na data
cadastrada. Deve aparecer um itinerário com **3 etapas, uma de cada
empresa** (identificadores globais `empresa:car-...`) — essa é a **busca
distribuída**. Reserve-o: a confirmação atravessa os três servidores em um
**2PC** e é atômica (todas as etapas ou nenhuma). Confira em "Minhas
reservas".

**Passo 3 — verificação do outro lado.** Abra o motorista da `expresso`
(`maria`) e liste as caronas (`2`): `ana@vaijunto` aparece como passageira
do trecho dela, embora `ana` nem exista na `expresso` — cada empresa segue
dona dos próprios dados.

**Passo 4 — queda e recuperação de um servidor** (o que o tutor fará na
apresentação):

```bash
docker compose stop rotasul
```

- A busca Salvador → Montes Claros passa a retornar **resultado parcial**
  com aviso de empresa indisponível (o itinerário completo some, pois
  dependia da `rotasul`).
- Reservas só com as empresas vivas continuam confirmando normalmente.
- Uma reserva envolvendo a empresa caída falha com **erro limpo em ~4 s**
  (timeout), sem travar e sem reservar pela metade.

```bash
docker compose start rotasul
```

Em alguns segundos o servidor relê o estado e o WAL, resolve as transações
pendentes consultando os coordenadores, e o itinerário completo volta a
aparecer — com a reserva de `ana` intacta.

Para assistir ao 2PC acontecendo, deixe `docker compose logs -f vaijunto`
aberto em um terminal ao lado durante a reserva.

---

## 4. Laboratório: um contêiner por máquina

Cenário exigido: cada empresa em um computador distinto. Em cada máquina,
suba **uma** empresa apontando `PARES` para os IPs das outras (descubra o
IPv4 de cada uma com `ipconfig`/`ip addr`).

Windows (cmd), na máquina da `expresso`, supondo `vaijunto` em
`10.0.0.41` e `rotasul` em `10.0.0.43`:

```bash
set EMPRESA=expresso&& set PARES=vaijunto=http://10.0.0.41:8000,rotasul=http://10.0.0.43:8002&& docker compose up expresso
```

PowerShell:

```bash
$env:EMPRESA="expresso"; $env:PARES="vaijunto=http://10.0.0.41:8000,rotasul=http://10.0.0.43:8002"; docker compose up expresso
```

Linux/macOS (bash):

```bash
EMPRESA=expresso PARES=vaijunto=http://10.0.0.41:8000,rotasul=http://10.0.0.43:8002 docker compose up expresso
```

As portas são publicadas no host (`ports`), então os servidores se alcançam
pelos IPs das máquinas, sem rede overlay. Os clientes de cada máquina usam
`VAIJUNTO_HOST=IP_DA_EMPRESA` (ou os argumentos host/porta sem Docker).

**Firewall (importante no laboratório):** a máquina de cada empresa precisa
aceitar entrada nas suas portas TCP (a de clientes e a REST). No Windows,
em um PowerShell **como administrador**:

```bash
New-NetFirewallRule -DisplayName 'VAIJUNTO' -Direction Inbound -Protocol TCP -LocalPort 5000-5002,8000-8002 -Action Allow
```

Teste a visibilidade entre máquinas com
`curl http://IP_DA_OUTRA:8000/ping` antes da demonstração.

---

## 5. Teste automatizado do consórcio (com injeção de falhas)

Com as três empresas no ar (Docker, uma máquina):

```bash
python -m testes.teste_consorcio --empresa vaijunto=127.0.0.1:5000 --empresa expresso=127.0.0.1:5001 --empresa rotasul=127.0.0.1:5002 --falhas
```

Quatro cenários (duração total ~80 s, por causa da janela de lease):

1. **Atomicidade:** 12 passageiros de 3 empresas disputam um itinerário de
   3 etapas com gargalo de 1 assento → exatamente 1 confirmação, nada pela
   metade.
2. **Exclusão mútua:** 18 passageiros pelos 3 servidores × 3 assentos →
   exatamente 3 confirmações.
3. **Deadlock distribuído:** reservas dos mesmos trechos em ordens opostas
   → todas as requisições terminam, capacidade respeitada.
4. **Injeção de falha** (`--falhas`, requer Docker): derruba a `rotasul`
   sob carga com `docker compose stop`, verifica que as sobreviventes
   seguem operando e que, após `start` e a janela de lease, nenhum assento
   fica preso e nenhum itinerário fica pela metade.

Saída esperada: `RESULTADO: todas as verificações passaram`.

O teste do Problema 1 segue válido contra uma empresa isolada:
`python -m testes.teste_concorrencia 127.0.0.1 5000`.

---

## 6. Simulador de carga

```bash
python -m simulador.simulador --tcp 127.0.0.1:5000 127.0.0.1:5001 127.0.0.1:5002 --passageiros 5 --duracao 30
```

Semeia caronas com rotas que cruzam empresas e dispara 5 passageiros por
empresa em ciclos de busca → reserva → cancelamento ocasional. Imprime
vazão e latências (média, p95, máx) de busca distribuída e reserva 2PC.
Conflitos "esgotado" são desfecho **correto** sob disputa, não erro.
Referência na máquina de desenvolvimento: ~190 ops/s, busca ~13 ms,
reserva 2PC ~350 ms.

---

## 7. Solução de problemas

| Sintoma | Causa provável / solução |
|---|---|
| `não foi possível conectar a HOST:PORTA` | Empresa não está no ar, host/porta errados, ou firewall bloqueando (ver Seção 4) |
| Reserva multi-empresa sempre falha | `PARES` com IP/porta errados, ou firewall entre as máquinas; teste `curl http://IP:8000/ping` de uma máquina para a outra |
| Latências de ~1-2 s rodando local no Windows | Usou `localhost` em `PARES`: o fallback IPv6 custa ~2 s por chamada; use `127.0.0.1` |
| Itinerário multi-empresa não aparece na busca | Datas diferentes entre as caronas, ou grafia das cidades não idêntica entre as empresas |
| Busca retorna aviso de empresa indisponível | Comportamento esperado com um servidor fora do ar: resultado parcial |
| Contêiner órfão `servidor-1` na lista | Sobra do Problema 1; remova com `docker compose down --remove-orphans` |
| Mudei código e o comportamento não mudou | Imagem velha: `docker compose build` explícito no serviço alterado |
| `Address already in use` | Porta ocupada por outra instância; pare-a ou mude `VAIJUNTO_PORTA`/`VAIJUNTO_PORTA_REST` |
| Quero zerar os dados | Pare os contêineres e apague o conteúdo de `dados/` (estado e WAL de cada empresa) |
