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

## Estrutura de diretórios

```
go-together/
├── comum/                      # pacote compartilhado por servidor e clientes
│   └── protocolo.py            #   enquadramento (4 bytes + JSON UTF-8) e validação
├── servidor/                   # pacote do servidor central
│   ├── servidor.py             #   socket TCP, um thread por cliente, despacho das operações
│   ├── estado.py               #   estado em memória, travas, reserva atômica, persistência
│   └── itinerarios.py          #   busca de itinerários (DFS combinando caronas)
├── cliente/                    # pacote dos clientes de terminal
│   ├── comum.py                #   conexão, requisições e fluxo de login (comum aos dois)
│   ├── motorista.py            #   cliente do motorista
│   └── passageiro.py           #   cliente do passageiro
├── testes/                     # pacote de testes automatizados
│   └── teste_concorrencia.py   #   concorrência, atomicidade, deadlock e desempenho
├── docs/
│   ├── PROTOCOLO.md            # especificação completa do protocolo de aplicação
│   └── TUTORIAL.md             # passo a passo de execução e roteiro de demonstração
├── dados/                      # criado em runtime: estado.json (persistência; fora do git)
├── Dockerfile                  # imagem única para todos os componentes
├── docker-compose.yml          # serviços: servidor, motorista, passageiro, teste
└── README.md
```

## Pacotes e dependências

O projeto usa **exclusivamente a biblioteca padrão do Python** — não há
`requirements.txt` nem `pip install`. Módulos utilizados e seus papéis:

| Módulo (stdlib) | Uso no projeto |
|---|---|
| `socket` | Comunicação TCP nativa (servidor e clientes) |
| `struct` | Empacotamento do cabeçalho de 4 bytes em big-endian |
| `json` | Representação intermediária das mensagens e persistência |
| `threading` | Um thread por cliente; `Lock`/`RLock` para o controle de concorrência; `Barrier` nos testes |
| `hashlib` | Hash SHA-256 das senhas |
| `uuid` | Geração de ids de caronas e reservas |
| `datetime` | Validação de datas e horas |
| `os` | Escrita atômica da persistência (`os.replace`) e variáveis de ambiente |
| `statistics`, `time` | Medições de latência no teste de carga |

## Como executar

Requisitos: Python 3.10+ (somente biblioteca padrão) ou Docker.
Passo a passo detalhado — incluindo o cenário multi-máquina do laboratório,
um roteiro de demonstração fim a fim e solução de problemas — em
[docs/TUTORIAL.md](docs/TUTORIAL.md).

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

## Como usar

Os dois clientes são interativos, guiados por menus numerados. Em ambos, o
primeiro passo é o acesso: `2` para registrar um novo usuário (nome + senha)
ou `1` para entrar com um usuário existente. A sessão dura enquanto a
conexão estiver aberta; `0` sai.

### Cliente motorista

```
=== VAIJUNTO - Motorista ===
1. Publicar carona
2. Minhas caronas (passageiros por trecho)
3. Cancelar carona
0. Sair
```

- **Publicar carona (1):** informe a rota como cidades separadas por vírgula
  e **na ordem do percurso** (ex.: `Salvador, Feira de Santana, Vitória da
  Conquista`), a data (`AAAA-MM-DD`), a hora (`HH:MM`), o número de assentos
  e o preço por trecho — um valor para cada trecho separado por vírgula
  (ex.: `40, 80`) ou um único valor aplicado a todos. O sistema responde com
  o id da carona.
- **Minhas caronas (2):** lista cada carona com seus trechos, mostrando por
  trecho o preço, os assentos livres e os passageiros confirmados.
- **Cancelar carona (3):** informe o id; as reservas de passageiros que
  usavam a carona são desfeitas por inteiro (e avisadas via status
  `cancelada_pelo_motorista` na consulta deles).

### Cliente passageiro

```
=== VAIJUNTO - Passageiro ===
1. Buscar itinerários
2. Reservar itinerário da última busca
3. Minhas reservas
4. Cancelar reserva
0. Sair
```

- **Buscar (1):** informe origem, destino e data. O sistema lista até 10
  itinerários do mais barato ao mais caro, cada um com suas etapas
  (motorista, horário, preço e assentos) — inclusive combinações de caronas
  de motoristas diferentes com baldeação.
- **Reservar (2):** escolha o número de um itinerário da última busca. A
  confirmação é atômica: todas as etapas ou nenhuma. Se outro passageiro
  confirmou antes e esgotou um trecho, o sistema recusa com o motivo —
  basta buscar novamente e escolher outra opção.
- **Minhas reservas (3):** lista id, data, status e etapas de cada reserva.
- **Cancelar reserva (4):** informe o id; os assentos de todas as etapas são
  liberados imediatamente.

Um roteiro de demonstração completo, com valores de exemplo, está em
[docs/TUTORIAL.md](docs/TUTORIAL.md).

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
