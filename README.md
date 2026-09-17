# VAIJUNTO — Sistema de Caronas Compartilhadas

Solução do Problema 1 da disciplina de Concorrência e Conectividade (UEFS):
um sistema de caronas de média e longa distância no qual motoristas publicam
rotas com assentos livres e passageiros reservam itinerários — possivelmente
combinando trechos de caronas de motoristas diferentes — com garantia de
atomicidade e sem venda dupla de assentos, tudo sobre **sockets TCP nativos**
(sem frameworks de mensageria ou RPC) e **controle de concorrência próprio**
(sem delegação a banco de dados ou serviço externo).

## Arquitetura

```
cliente motorista ──┐                       ┌── Estado em memória
                    ├── TCP (porta 5000) ── │   (travas por carona)
cliente passageiro ─┘    servidor central   └── Persistência JSON (snapshot atômico)
```

| Componente | Descrição |
|---|---|
| [servidor/servidor.py](servidor/servidor.py) | Servidor TCP concorrente: um thread por cliente, despacho das operações, tratamento de falhas e timeouts |
| [servidor/estado.py](servidor/estado.py) | O "banco de dados": estado em memória com travas (global + por carona), reserva atômica com travamento ordenado, persistência JSON |
| [servidor/itinerarios.py](servidor/itinerarios.py) | Busca de itinerários (DFS sobre o grafo de cidades, combinando caronas) |
| [comum/protocolo.py](comum/protocolo.py) | Enquadramento das mensagens: prefixo de 4 bytes + JSON UTF-8 |
| [cliente/motorista.py](cliente/motorista.py) | Cliente de terminal do motorista |
| [cliente/passageiro.py](cliente/passageiro.py) | Cliente de terminal do passageiro |
| [testes/teste_concorrencia.py](testes/teste_concorrencia.py) | Teste automatizado de concorrência, atomicidade, deadlock e desempenho |
| [docs/PROTOCOLO.md](docs/PROTOCOLO.md) | Especificação completa do protocolo de aplicação |

### Decisões de projeto (resumo)

- **Modelo de dados por trecho:** uma rota de N cidades define N−1 trechos;
  a ocupação é contada por trecho (`ocupados[k]`), de modo que um assento
  usado só no início da rota continua disponível nos trechos seguintes.
- **Concorrência:** trava global curta para os índices + **uma trava por
  carona**. Reservas em caronas distintas prosseguem em paralelo.
- **Atomicidade:** na confirmação, as travas de todas as caronas do
  itinerário são adquiridas em **ordem global de id** (elimina deadlock por
  quebra da espera circular); todas as verificações de disponibilidade
  acontecem **antes** de qualquer escrita — tudo ou nada.
- **Sem assentos presos:** a reserva é efetivada em **uma única requisição
  atômica**; não há estado pendente entre mensagens, então a queda abrupta
  de um cliente nunca deixa assento bloqueado.
- **Persistência:** snapshot JSON gravado de forma atômica (arquivo
  temporário + rename) após cada mutação; recarregado no boot do servidor.

## Como executar

Requisitos: Python 3.10+ (somente biblioteca padrão) ou Docker.

### Local (sem Docker)

```bash
python -m servidor.servidor
```

Em outros terminais (host/porta opcionais; padrão `localhost 5000`):

```bash
python -m cliente.motorista
```

```bash
python -m cliente.passageiro
```

### Com Docker (uma máquina)

```bash
docker compose up --build servidor
```

```bash
docker compose run --rm motorista
```

```bash
docker compose run --rm passageiro
```

### Em máquinas distintas (laboratório)

Na máquina do servidor, `docker compose up servidor` publica a porta 5000 no
host (`ports: "5000:5000"`), tornando o serviço alcançável pelo IP da máquina
na rede local. Nas máquinas dos clientes, basta apontar para esse IP:

```bash
VAIJUNTO_HOST=IP_DO_SERVIDOR docker compose run --rm passageiro
```

ou, sem Docker: `python -m cliente.passageiro IP_DO_SERVIDOR 5000`.

## Teste de concorrência

Com o servidor no ar:

```bash
python -m testes.teste_concorrencia
```

ou `docker compose run --rm teste`. O teste dispara dezenas de clientes TCP
simultâneos (liberados juntos por uma barreira) e verifica:

1. **Sem venda dupla** — 30 passageiros disputam 5 assentos do mesmo trecho;
   exatamente 5 confirmam.
2. **Atomicidade** — 20 passageiros disputam um itinerário de 2 caronas com
   1 vaga no gargalo; exatamente 1 confirma e nenhuma reserva fica "pela
   metade" na outra carona.
3. **Sem deadlock** — itinerários que atravessam as mesmas caronas em ordens
   opostas; o teste falha por timeout se o servidor travar.
4. **Desempenho** — tempo de resposta (média/p95/máx) de reservas e de 200
   buscas sob carga.

## Protocolo

Mensagens JSON (UTF-8) com prefixo de tamanho de 4 bytes (big-endian) sobre
TCP. Especificação completa, com formato das mensagens, fluxo de conexão,
todas as operações e exemplos: [docs/PROTOCOLO.md](docs/PROTOCOLO.md).
