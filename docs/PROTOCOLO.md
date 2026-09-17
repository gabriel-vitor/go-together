# Protocolo de Aplicação do VAIJUNTO

Especificação do protocolo de comunicação entre os clientes (motorista e
passageiro) e o servidor central, implementado diretamente sobre a API de
sockets TCP/IP, sem frameworks de mensageria ou RPC.

## 1. Transporte

- **Protocolo:** TCP, porta padrão **5000**.
- **Justificativa:** o domínio exige entrega confiável e ordenada — a perda
  ou reordenação de uma confirmação de reserva seria inaceitável. O TCP
  fornece essas garantias no transporte, deixando para a aplicação apenas o
  enquadramento e a semântica das mensagens.
- Cada cliente mantém **uma conexão persistente** com o servidor durante toda
  a sessão. O servidor atende cada conexão em um thread dedicado.

## 2. Enquadramento (framing)

O TCP entrega um fluxo de bytes sem fronteiras de mensagem. Cada mensagem do
VAIJUNTO é transmitida como:

```
+------------------+--------------------------------------+
| 4 bytes          | N bytes                              |
| uint32 big-endian| corpo: objeto JSON codificado UTF-8  |
| = N (tamanho)    |                                      |
+------------------+--------------------------------------+
```

- Cabeçalho em **big-endian** (network byte order), independente da
  arquitetura das máquinas.
- Tamanho máximo do corpo: **1 MiB**. Valores `0` ou acima do máximo são
  erro **fatal** de enquadramento: o receptor encerra a conexão, pois o
  fluxo perdeu o sincronismo.
- Corpo que não é um objeto JSON válido é erro **recuperável**: o servidor
  descarta a mensagem, responde `{"status": "erro", ...}` e mantém a conexão.

**Exemplo em bytes** — a mensagem `{"op":"ping"}` (13 bytes de corpo):

```
00 00 00 0D 7B 22 6F 70 22 3A 22 70 69 6E 67 22 7D
└─tamanho─┘ └────────────── corpo JSON ───────────┘
```

## 3. Representação dos dados

- **JSON** (RFC 8259) codificado em **UTF-8**: representação intermediária
  textual e autodescritiva, com suporte nativo em praticamente qualquer
  linguagem — clientes escritos em linguagens diferentes interoperam com o
  servidor sem bibliotecas especiais.
- O receptor **valida tudo o que chega**: tipo de cada campo, tamanhos
  máximos, formatos de data (`AAAA-MM-DD`) e hora (`HH:MM`). Mensagens
  malformadas são descartadas com resposta de erro.

## 4. Modelo de interação

Requisição-resposta síncrono sobre a mesma conexão: o cliente envia uma
requisição e aguarda **exatamente uma** resposta antes da próxima.

- **Requisição:** objeto JSON com o campo obrigatório `"op"` (string com o
  nome da operação) mais os campos específicos da operação.
- **Resposta de sucesso:** `{"status": "ok", "op": "<op>", "dados": ...}`
- **Resposta de erro:** `{"status": "erro", "op": "<op>", "erro": "<motivo>"}`

## 5. Fluxo de conexão, autenticação e desconexão

```
cliente                                  servidor
   │ ── TCP connect ──────────────────────▶ │  accept(); cria thread da conexão
   │ ── {"op":"registrar", ...} ──────────▶ │  (opcional, primeira vez)
   │ ◀───────────── {"status":"ok", ...} ── │
   │ ── {"op":"login", ...} ──────────────▶ │  autentica e marca a SESSÃO
   │ ◀───────────── {"status":"ok", ...} ── │
   │ ── operações autenticadas ... ───────▶ │
   │ ◀── respostas ... ─────────────────────│
   │ ── FIN (fecha o socket) ─────────────▶ │  encerra o thread da conexão
```

- A **sessão é a própria conexão TCP**: após o `login`, o servidor associa o
  usuário àquela conexão; não há token. Fechar a conexão encerra a sessão.
- Toda operação (exceto `registrar`, `login` e `ping`) exige sessão
  autenticada; caso contrário o servidor responde erro.
- **Desconexão abrupta** (queda do cliente): o servidor detecta pelo
  `recv` (retorno vazio ou exceção), fecha o socket e encerra apenas aquele
  thread. Como toda mutação de estado é atômica dentro de uma única
  requisição, não existem reservas "pela metade" a limpar.
- **Timeout de inatividade:** conexões sem tráfego por 600 s são encerradas
  pelo servidor.

## 6. Operações

### 6.1 `ping` (pública)

Verificação de vida. Requisição: `{"op": "ping"}` →
`{"status":"ok","op":"ping","dados":{"pong":true}}`

### 6.2 `registrar` (pública)

| Campo    | Tipo   | Regras                       |
|----------|--------|------------------------------|
| usuario  | string | 1–40 caracteres, único       |
| senha    | string | mínimo 4 caracteres          |

```json
{"op": "registrar", "usuario": "joao", "senha": "1234"}
{"status": "ok", "op": "registrar", "dados": {"mensagem": "usuário registrado com sucesso"}}
```

Erros: usuário já existe; campos ausentes/inválidos.

### 6.3 `login` (pública)

```json
{"op": "login", "usuario": "joao", "senha": "1234"}
{"status": "ok", "op": "login", "dados": {"usuario": "joao"}}
```

Erro: `{"status":"erro","op":"login","erro":"usuário ou senha incorretos"}`

### 6.4 `publicar_carona` (motorista)

| Campo    | Tipo          | Regras                                          |
|----------|---------------|-------------------------------------------------|
| rota     | lista[string] | 2–20 cidades, ordenadas, sem repetição          |
| data     | string        | `AAAA-MM-DD`                                    |
| hora     | string        | `HH:MM`                                         |
| assentos | inteiro       | 1–50                                            |
| precos   | lista[número] | exatamente `len(rota)-1` valores, um por trecho |

Uma rota de N cidades define **N−1 trechos**; o trecho *k* liga `rota[k]` a
`rota[k+1]`. A disponibilidade é controlada por trecho.

```json
{"op": "publicar_carona",
 "rota": ["Salvador", "Feira de Santana", "Vitória da Conquista"],
 "data": "2026-09-25", "hora": "08:00",
 "assentos": 3, "precos": [40.0, 80.0]}
```

```json
{"status": "ok", "op": "publicar_carona", "dados": {
  "id": "car-1a2b3c4d5e", "motorista": "joao",
  "rota": ["Salvador", "Feira de Santana", "Vitória da Conquista"],
  "data": "2026-09-25", "hora": "08:00", "assentos": 3, "cancelada": false,
  "trechos": [
    {"trecho": "Salvador -> Feira de Santana", "preco": 40.0, "disponiveis": 3},
    {"trecho": "Feira de Santana -> Vitória da Conquista", "preco": 80.0, "disponiveis": 3}
  ]}}
```

### 6.5 `minhas_caronas` (motorista)

Requisição: `{"op": "minhas_caronas"}`. Resposta: lista de caronas do
motorista no formato acima, com o campo adicional `passageiros` (lista de
nomes) em cada trecho.

### 6.6 `cancelar_carona` (motorista)

```json
{"op": "cancelar_carona", "carona_id": "car-1a2b3c4d5e"}
{"status": "ok", "op": "cancelar_carona",
 "dados": {"carona_id": "car-1a2b3c4d5e", "reservas_canceladas": 2}}
```

Cancela a carona e desfaz **por inteiro** as reservas que a usavam (os
assentos das outras caronas desses itinerários também são liberados), com
status `cancelada_pelo_motorista`. Somente o motorista dono pode cancelar.

### 6.7 `buscar_itinerarios` (passageiro)

| Campo   | Tipo   | Regras       |
|---------|--------|--------------|
| origem  | string | cidade       |
| destino | string | cidade       |
| data    | string | `AAAA-MM-DD` |

```json
{"op": "buscar_itinerarios", "origem": "Salvador",
 "destino": "Vitória da Conquista", "data": "2026-09-25"}
```

```json
{"status": "ok", "op": "buscar_itinerarios", "dados": [
  {"preco_total": 95.0, "num_etapas": 2, "etapas": [
     {"carona_id": "car-1a2b3c4d5e", "motorista": "joao",
      "origem": "Salvador", "destino": "Feira de Santana",
      "data": "2026-09-25", "hora": "08:00", "preco": 40.0,
      "assentos_disponiveis": 3},
     {"carona_id": "car-9f8e7d6c5b", "motorista": "maria",
      "origem": "Feira de Santana", "destino": "Vitória da Conquista",
      "data": "2026-09-25", "hora": "12:00", "preco": 55.0,
      "assentos_disponiveis": 1}]}
]}
```

Itinerários podem combinar até **3 etapas** de caronas (e motoristas)
diferentes. Resultados ordenados por preço total, número de etapas e hora de
partida; no máximo 10 opções. A busca é **consultiva**: a disponibilidade só
é garantida na confirmação (`reservar`).

### 6.8 `reservar` (passageiro)

| Campo  | Tipo         | Regras                                             |
|--------|--------------|----------------------------------------------------|
| etapas | lista[objeto]| 1–4 etapas; cada uma com `carona_id`, `origem`, `destino` |

O itinerário deve ser contíguo (destino de uma etapa = origem da seguinte) e
com todas as etapas na mesma data. A confirmação é **atômica**: ou todas as
etapas são reservadas, ou nenhuma.

```json
{"op": "reservar", "etapas": [
  {"carona_id": "car-1a2b3c4d5e", "origem": "Salvador", "destino": "Feira de Santana"},
  {"carona_id": "car-9f8e7d6c5b", "origem": "Feira de Santana", "destino": "Vitória da Conquista"}]}
```

```json
{"status": "ok", "op": "reservar", "dados": {
  "id": "res-0a1b2c3d4e", "passageiro": "ana", "status": "confirmada",
  "criada_em": "2026-09-17T14:03:22", "data": "2026-09-25",
  "preco_total": 95.0, "etapas": [ ... ]}}
```

Erro típico quando outro passageiro confirmou antes (o primeiro a confirmar
mantém a preferência):

```json
{"status": "erro", "op": "reservar",
 "erro": "assentos esgotados no trecho Feira de Santana -> Vitória da Conquista (carona car-9f8e7d6c5b)"}
```

### 6.9 `minhas_reservas` (passageiro)

Requisição: `{"op": "minhas_reservas"}`. Resposta: lista de reservas do
usuário (mais recentes primeiro), cada uma com `status`
(`confirmada`, `cancelada` ou `cancelada_pelo_motorista`), etapas e preço.

### 6.10 `cancelar_reserva` (passageiro)

```json
{"op": "cancelar_reserva", "reserva_id": "res-0a1b2c3d4e"}
{"status": "ok", "op": "cancelar_reserva",
 "dados": {"reserva_id": "res-0a1b2c3d4e", "status": "cancelada"}}
```

Libera os assentos de todas as etapas. Apenas o dono da reserva pode cancelar.

## 7. Sincronização e regras de consistência

- **Um assento por trecho nunca é vendido duas vezes:** o servidor
  re-verifica a disponibilidade de todos os trechos dentro de uma seção
  crítica (travas por carona) antes de gravar qualquer reserva.
- **Atomicidade:** as travas de todas as caronas do itinerário são adquiridas
  em **ordem global de id** (prevenção de deadlock por ordenação total de
  recursos); todas as verificações precedem qualquer escrita.
- **Sem bloqueio permanente:** não existe estado "reserva pendente" entre
  mensagens — a reserva é efetivada em uma única requisição atômica. A queda
  de um cliente, portanto, nunca deixa assentos presos.
