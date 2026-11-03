# VAIJUNTO — Consórcio de Caronas Compartilhadas

Solução do **Problema 2** da disciplina TEC502 — Concorrência e Conectividade
(UEFS): um consórcio de empresas de carona em que **cada empresa mantém o seu
próprio servidor** (dados, relógio e fila de requisições próprios), e um
passageiro de qualquer empresa reserva itinerários que combinam caronas de
**todas** elas — com atomicidade distribuída, sem venda dupla de assentos e
tolerando a queda de qualquer servidor.

> A versão do **Problema 1** (servidor central único) está preservada na tag
> [`problema-1`](../../tree/problema-1).

## Arquitetura

```
  passageiros/motoristas da empresa  (protocolo TCP próprio — Problema 1)
                 │
        ┌────────▼────────┐   API REST (2PC, busca, status)   ┌─────────────┐
        │   servidor da   │◄─────────────────────────────────►│  servidor   │
        │    "vaijunto"   │                                   │ "expresso"  │
        │ estado + travas │◄──────────────┐                   └──────▲──────┘
        │ WAL + leases    │               │                          │
        └─────────────────┘        ┌──────▼──────┐                   │
                                   │  servidor   │◄──────────────────┘
                                   │  "rotasul"  │
                                   └─────────────┘
```

Cada servidor de empresa é o servidor do Problema 1 (TCP para os próprios
clientes) acrescido de uma camada de consórcio:

| Mecanismo | Como foi resolvido |
|---|---|
| **Exclusão mútua distribuída** | Princípio do **dono único**: cada carona pertence a uma empresa; só o servidor dela escreve na ocupação, sob a trava local — não existe trava distribuída sobre o assento |
| **Atomicidade distribuída** | **Two-Phase Commit**: o servidor do passageiro coordena; participantes votam no PREPARE criando *holds* (assentos retidos com **lease**); decisão vai ao **WAL** antes de ser comunicada; COMMIT/ABORT idempotentes |
| **Precedência** | **Relógio lógico de Lamport**: a chave `(contador, empresa)` dá ordem total entre transações, independente dos relógios físicos |
| **Deadlock distribuído** | PREPAREs em **ordem canônica** de empresa + **wait-die** com a chave de Lamport (mais antiga espera, mais nova morre) |
| **Tolerância a falhas** | Timeouts em toda chamada REST; *lease* expira e libera assentos se o coordenador sumir; participante em dúvida consulta `GET /transacoes/{tx}`; coordenador reenvia decisões pendentes ao religar (WAL) |
| **Busca distribuída** | **Dispersar-coletar** em tempo real: consulta paralela ao catálogo de cada par, com resultado parcial se uma empresa estiver fora do ar |

## Componentes

| Caminho | Papel |
|---|---|
| [servidor/](servidor/) | Servidor de empresa: TCP para clientes, estado em memória com travas por carona, holds/leases, persistência JSON atômica |
| [consorcio/nucleo.py](consorcio/nucleo.py) | Coordenador 2PC, wait-die, varredor de leases/pendências, recuperação pós-queda |
| [consorcio/rest_api.py](consorcio/rest_api.py) | API REST entre servidores (FastAPI) — espec. em [docs/API-REST.md](docs/API-REST.md) |
| [consorcio/lamport.py](consorcio/lamport.py) / [wal.py](consorcio/wal.py) / [pares.py](consorcio/pares.py) | Relógio lógico, write-ahead log e cliente HTTP com timeouts |
| [cliente/](cliente/) | Clientes motorista e passageiro (reaproveitados do Problema 1; conectam só à própria empresa) |
| [simulador/simulador.py](simulador/simulador.py) | Simulador de passageiros: carga autônoma de buscas/reservas pelas 3 empresas |
| [testes/teste_consorcio.py](testes/teste_consorcio.py) | Teste do consórcio: atomicidade, exclusão mútua e deadlock entre servidores + **injeção de falhas** |
| [testes/teste_concorrencia.py](testes/teste_concorrencia.py) | Teste do Problema 1 (segue válido contra uma empresa isolada) |
| [docs/PROTOCOLO.md](docs/PROTOCOLO.md) | Protocolo TCP cliente↔servidor (Problema 1, ainda em uso) |
| [docs/API-REST.md](docs/API-REST.md) | **Especificação da API REST** do consórcio, com diagrama de sequência do 2PC |

**Dependências:** Python 3.10+; `fastapi`, `uvicorn` e `requests` (permitidos para a API
REST — `pip install -r requirements.txt`). A coordenação distribuída é toda
implementação própria, sem ZooKeeper/etcd/Redis/Consul/XA.

## Como executar

### Consórcio completo em uma máquina (Docker)

```bash
docker compose up --build -d vaijunto expresso rotasul
```

Sobe as três empresas: `vaijunto` (TCP 5000/REST 8000), `expresso`
(5001/8001) e `rotasul` (5002/8002), cada uma com volume de dados próprio.

Clientes (escolha a empresa com `VAIJUNTO_HOST`):

```bash
docker compose run --rm -e VAIJUNTO_HOST=expresso passageiro
```

ou, sem Docker: `python -m cliente.passageiro 127.0.0.1 5001`.

### Em máquinas distintas (laboratório)

Cada máquina sobe **uma** empresa, apontando `PARES` para os IPs das outras:

```bash
set EMPRESA=expresso && set PARES=vaijunto=http://10.0.0.41:8000,rotasul=http://10.0.0.43:8002 && docker compose up expresso
```

(as portas 5000–5002 e 8000–8002 são publicadas no host; use o IP real de
cada máquina — em redes Windows locais prefira IPs a `localhost`).

### Simulando a queda de um servidor

```bash
docker compose stop rotasul
```

As demais empresas seguem operando (buscas parciais, reservas locais);
reservas envolvendo a empresa caída falham com erro limpo em ~4 s.
`docker compose start rotasul` recupera: o servidor relê estado + WAL,
resolve holds pendentes consultando os coordenadores e o consórcio volta
consistente.

## Testes e simulação

Teste do consórcio (3 cenários distribuídos; com Docker, `--falhas` liga a
injeção de falha):

```bash
python -m testes.teste_consorcio --empresa vaijunto=127.0.0.1:5000 --empresa expresso=127.0.0.1:5001 --empresa rotasul=127.0.0.1:5002 --falhas
```

Verifica: atomicidade com gargalo de 1 assento entre 3 empresas (exatamente
1 confirmação, nada pela metade); exclusão mútua com 18 passageiros de 3
servidores disputando 3 assentos; deadlock com ordens opostas entre 2
empresas; e, sob falha injetada, que nenhum assento fica preso e nenhum
itinerário fica pela metade após a recuperação.

Simulador de carga:

```bash
python -m simulador.simulador --tcp 127.0.0.1:5000 127.0.0.1:5001 127.0.0.1:5002 --passageiros 5 --duracao 30
```

Medições em uma máquina (3 empresas em contêineres, dados zerados,
15 passageiros simultâneos): ~192 operações/s, busca distribuída com média
de 13 ms e reserva 2PC com média de 355 ms (inclui as esperas do wait-die
sob disputa; sem concorrência a reserva fica em ~165 ms).
