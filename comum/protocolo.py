"""
Camada de protocolo do VAIJUNTO (enquadramento + codificação).

O TCP entrega um FLUXO contínuo de bytes, sem noção de "mensagem". Para que
cliente e servidor saibam onde uma mensagem termina e a próxima começa,
adotamos enquadramento por prefixo de tamanho (length-prefix framing):

    +----------------+----------------------------------------+
    | 4 bytes        | N bytes                                |
    | uint32 big-    | corpo: objeto JSON codificado em UTF-8 |
    | endian = N     |                                        |
    +----------------+----------------------------------------+

Escolhas de projeto:
  * JSON como representação intermediária: é textual, autodescritiva e tem
    suporte nativo em praticamente todas as linguagens, permitindo que
    clientes escritos em linguagens diferentes interoperem com o servidor.
  * Big-endian (network byte order) no cabeçalho, como é convenção em
    protocolos de rede, para independer da arquitetura das máquinas.
  * Limite máximo de mensagem para impedir que um cliente malicioso ou
    defeituoso faça o servidor alocar memória sem controle.

O receptor é responsável por validar tudo o que chega: cabeçalho com tamanho
inválido encerra a conexão (o fluxo perdeu o sincronismo); corpo que não é
JSON válido é descartado e respondido com erro (o enquadramento continua
íntegro, então a conexão pode ser mantida).
"""

import json
import socket
import struct

# Cabeçalho: inteiro de 4 bytes sem sinal, big-endian ("!" = network order).
_CABECALHO = struct.Struct("!I")

# Tamanho máximo aceito para o corpo de uma mensagem (1 MiB).
TAMANHO_MAXIMO = 1024 * 1024


class ErroEnquadramento(Exception):
    """Erro FATAL de enquadramento: o fluxo de bytes perdeu o sincronismo
    (ex.: cabeçalho anuncia tamanho impossível). A conexão deve ser fechada."""


class MensagemInvalida(Exception):
    """Erro RECUPERÁVEL: o corpo chegou íntegro, mas não é um objeto JSON
    válido. A mensagem é descartada e a conexão pode continuar em uso."""


def enviar(sock: socket.socket, mensagem: dict) -> None:
    """Serializa `mensagem` em JSON e a envia com o cabeçalho de tamanho.

    `sendall` garante que todos os bytes serão escritos no socket (ou que
    uma exceção será lançada), diferentemente de `send`, que pode escrever
    apenas parte do buffer.
    """
    corpo = json.dumps(mensagem, ensure_ascii=False).encode("utf-8")
    if len(corpo) > TAMANHO_MAXIMO:
        raise ValueError(f"mensagem excede o tamanho máximo ({TAMANHO_MAXIMO} bytes)")
    sock.sendall(_CABECALHO.pack(len(corpo)) + corpo)


def _receber_exato(sock: socket.socket, n: int) -> bytes | None:
    """Lê exatamente `n` bytes do socket, acumulando leituras parciais.

    Retorna None se o outro lado fechou a conexão antes de completar os
    `n` bytes (recv devolve b"" em conexão encerrada).
    """
    partes = bytearray()
    while len(partes) < n:
        pedaco = sock.recv(n - len(partes))
        if not pedaco:
            return None
        partes.extend(pedaco)
    return bytes(partes)


def receber(sock: socket.socket) -> dict | None:
    """Lê uma mensagem completa do socket.

    Retorna:
      * dict  -> mensagem recebida e validada;
      * None  -> o outro lado encerrou a conexão de forma limpa.

    Lança:
      * ErroEnquadramento -> tamanho inválido no cabeçalho (fechar conexão);
      * MensagemInvalida  -> corpo não é um objeto JSON válido (descartar).
    """
    cabecalho = _receber_exato(sock, _CABECALHO.size)
    if cabecalho is None:
        return None
    (tamanho,) = _CABECALHO.unpack(cabecalho)
    if tamanho == 0 or tamanho > TAMANHO_MAXIMO:
        raise ErroEnquadramento(f"tamanho de mensagem inválido: {tamanho}")

    corpo = _receber_exato(sock, tamanho)
    if corpo is None:
        # Conexão caiu no meio de uma mensagem: tratamos como encerramento.
        return None

    try:
        mensagem = json.loads(corpo.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MensagemInvalida(f"corpo não é JSON válido: {exc}") from exc
    if not isinstance(mensagem, dict):
        raise MensagemInvalida("a mensagem deve ser um objeto JSON")
    return mensagem
