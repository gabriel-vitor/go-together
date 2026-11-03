# API REST do Consórcio VAIJUNTO

Especificação da interface que cada **servidor de empresa** oferece aos
**demais servidores do consórcio** (Problema 2). A comunicação com os
clientes motorista e passageiro permanece no protocolo TCP próprio do
Problema 1 ([PROTOCOLO.md](PROTOCOLO.md)); esta API é exclusivamente
servidor↔servidor.

## 1. Visão geral

- **Transporte:** HTTP/1.1; corpo das mensagens em **JSON (UTF-8)**.
- **Por que REST:** exigência do problema — tráfego HTTP em porta conhecida
  atravessa os firewalls das empresas do consórcio.
- **Porta padrão:** 8000 (configurável por `VAIJUNTO_PORTA_REST`).
- **Framework:** Flask (permitido pelo enunciado). Os mecanismos de
  coordenação — 2PC, exclusão mútua, wait-die, recuperação — são
  implementação própria (pacote [consorcio/](../consorcio/)).
- **Relógio lógico:** toda requisição e resposta carrega o cabeçalho
  `X-Lamport` (inteiro). O receptor ajusta seu relógio para
  `max(local, recebido) + 1`, mantendo a ordem causal entre os servidores.
  `X-Empresa` identifica o remetente.
- **Timeouts:** quem chama usa timeout curto (1 s de conexão, 4 s de
  leitura). Um par que não responde é tratado como indisponível — a queda
  de uma empresa nunca é erro fatal para as demais.
- **Idempotência:** todas as operações de transação são chaveadas pelo
  `tx` (id da transação). **Repetir uma requisição devolve o mesmo
  resultado e nunca aplica o efeito duas vezes** — o que torna seguro
  reenviar após um timeout sem saber se a original chegou.

## 2. Identificadores

- **Transação:** `tx-<empresa-coordenadora>-<10 hex>` (ex.:
  `tx-vaijunto-9ee5f1fc55`).
- **Carona global:** `<empresa>:<id local>` (ex.: `expresso:car-1a2b3c4d5e`).
  Cada carona pertence a UMA empresa (dono único), que é a única a escrever
  na sua ocupação.
- **Chave de precedência:** `[relogio_lamport, empresa]` — ordem total que
  decide disputas pelo último assento e alimenta o wait-die.

## 3. Recursos e métodos

### 3.1 `GET /ping`

Verificação de vida. **200**: `{"empresa": "vaijunto", "lamport": 42}`

### 3.2 `GET /caronas?data=AAAA-MM-DD`

Catálogo local da empresa para a **busca distribuída** (dispersar-coletar):
o servidor do passageiro consulta todos os pares em paralelo e agrega.

**200**:
```json
{"empresa": "expresso", "caronas": [
  {"id": "car-9f8e7d6c5b", "motorista": "maria",
   "rota": ["Feira de Santana", "Vitória da Conquista"],
   "data": "2026-12-20", "hora": "12:00",
   "precos": [55.0], "disponiveis": [2]}
]}
```
**400**: data inválida.

### 3.3 `POST /transacoes/{tx}/prepare` — 2PC fase 1

O coordenador pede que o participante **retenha** os assentos das etapas
cujas caronas pertencem a ele. Voto "sim" cria um **hold com lease**
(padrão 30 s): os assentos ficam retidos aguardando commit/abort; se o
coordenador sumir, o lease expira e os assentos voltam.

**Requisição:**
```json
{"chave": [17, "vaijunto"], "coordenador": "vaijunto",
 "passageiro": "ana@vaijunto",
 "etapas": [{"carona_id": "car-9f8e7d6c5b",
             "origem": "Feira de Santana",
             "destino": "Vitória da Conquista"}]}
```

| Código | Corpo | Significado |
|---|---|---|
| 200 | `{"voto": "sim", "etapas": [resolvidas]}` | assentos retidos (hold criado) |
| 409 | `{"voto": "nao", "motivo": "..."}` | indisponível em definitivo |
| 423 | `{"voto": "aguarde"}` | retido por transação MAIS NOVA; o coordenador (transação mais antiga) espera e repete — **wait-die** |

Repetição: o mesmo `prepare` devolve o voto já dado (idempotente).

### 3.4 `POST /transacoes/{tx}/commit` — 2PC fase 2

Converte o hold em reserva confirmada.

| Código | Corpo | Significado |
|---|---|---|
| 200 | `{"resultado": "efetivada", "reserva_id": "res-..."}` | confirmado (idempotente) |
| 410 | `{"erro": "transação desconhecida ou expirada"}` | o hold expirou/não existe — o coordenador dispara a **compensação** |

### 3.5 `POST /transacoes/{tx}/abort` — 2PC fase 2

Libera o hold e devolve os assentos. **200** sempre
(`{"resultado": "abortada"}`): abortar o inexistente é sucesso idempotente,
e a transação fica marcada para recusar um `prepare` atrasado.

### 3.6 `GET /transacoes/{tx}` — consulta ao coordenador

Usada pelos **participantes em recuperação**: um hold cujo lease venceu não
é liberado às cegas — o participante pergunta o desfecho ao coordenador.

**200**: `{"tx": "...", "estado": "pendente" | "efetivada" | "abortada" | "desconhecida"}`
(`desconhecida` ⇒ presunção de aborto, pois o coordenador grava o início no
WAL antes do primeiro prepare.)

### 3.7 `DELETE /reservas/{tx}`

Cancela (pós-commit) a reserva local da transação — usado pelo cancelamento
distribuído de itinerário e pela compensação. **200** idempotente:
`{"tx": "...", "status": "cancelada" | "inexistente"}`

### 3.8 `POST /notificacoes/cancelamento`

Um motorista cancelou a carona de uma etapa; a empresa dona avisa o
**coordenador** da transação, que desfaz o itinerário INTEIRO nas demais
empresas (a atomicidade vale também no desfazer).
Corpo: `{"tx": "...", "motivo": "cancelada_pelo_motorista"}` → **200**.

## 4. Diagrama de sequência — confirmação multi-empresa

Passageiro da `vaijunto` reserva itinerário com etapas na `vaijunto` (local),
`expresso` e `rotasul`:

```
cliente          vaijunto                 expresso              rotasul
(TCP)         (coordenador)            (participante)        (participante)
  │ reservar       │                        │                     │
  │───────────────>│ WAL: inicio(tx)        │                     │
  │                │ chave=[Lamport,emp]    │                     │
  │                │                        │                     │
  │                │ PREPARE (local: hold próprio)                │
  │                │───────── prepare ─────>│                     │
  │                │<──── 200 voto=sim ─────│  (hold + lease)     │
  │                │────────────────── prepare ──────────────────>│
  │                │<───────────────── 200 voto=sim ──────────────│
  │                │                        │                     │
  │                │ WAL: decisao=EFETIVADA │ <- decidido ANTES   │
  │                │                        │    de comunicar     │
  │                │ COMMIT (local)         │                     │
  │                │───────── commit ──────>│  hold -> reserva    │
  │                │<──────── 200 ──────────│                     │
  │                │────────────────── commit ───────────────────>│
  │                │<───────────────── 200 ───────────────────────│
  │                │ WAL: concluida         │                     │
  │<─ confirmada ──│                        │                     │
```

**Caminho de aborto** (ex.: `rotasul` sem assento ou fora do ar): o voto
"não"/timeout faz o coordenador gravar `decisao=ABORTADA` e enviar `abort`
aos já preparados — os holds são desfeitos e nada é confirmado pela metade.

**Falhas:**
- *Participante caiu no prepare* → timeout vira voto "não" → aborto global.
- *Coordenador caiu antes da decisão* → leases expiram; participantes
  consultam `GET /transacoes/{tx}`; "desconhecida/abortada" ⇒ liberam.
- *Coordenador caiu após a decisão* → na volta, o WAL manda reenviar os
  commits/aborts pendentes (idempotentes) até concluir.
- *Participante caiu entre prepare e commit* → na volta, recarrega o hold
  do seu estado persistido e resolve pelo mesmo `GET /transacoes/{tx}`.
