"""
Infraestrutura comum aos clientes de terminal (motorista e passageiro):
conexão TCP, envio de requisições e fluxo de autenticação.
"""

import os
import socket
import sys

from comum import protocolo

TEMPO_LIMITE_RESPOSTA = 30  # segundos aguardando resposta do servidor


class ErroServidor(Exception):
    """Resposta {"status": "erro"} vinda do servidor."""


def entrada(rotulo: str = "") -> str:
    """input() tolerante a bytes inválidos vindos do terminal.

    Em terminais Windows (e através do TTY do Docker), um acento ou tecla
    morta pode chegar como byte que não é UTF-8 válido; o Python o representa
    com um substituto (surrogate) que explode na hora de serializar o JSON.
    Aqui o substituto vira '?' em vez de derrubar o cliente.
    """
    return input(rotulo).encode("utf-8", "replace").decode("utf-8")


class Conexao:
    """Conexão persistente de um cliente com o servidor VAIJUNTO."""

    def __init__(self, host: str, porta: int):
        self.sock = socket.create_connection((host, porta), timeout=TEMPO_LIMITE_RESPOSTA)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def pedir(self, op: str, **campos) -> dict:
        """Envia uma requisição e aguarda a resposta correspondente.

        O protocolo é requisição-resposta sobre a mesma conexão: cada
        mensagem enviada gera exatamente uma resposta do servidor.
        """
        protocolo.enviar(self.sock, {"op": op, **campos})
        resposta = protocolo.receber(self.sock)
        if resposta is None:
            raise ConnectionError("o servidor encerrou a conexão")
        if resposta.get("status") != "ok":
            raise ErroServidor(resposta.get("erro", "erro desconhecido"))
        return resposta.get("dados")

    def fechar(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def endereco_servidor() -> tuple[str, int]:
    """Host/porta do servidor: argumentos de linha de comando ou variáveis
    de ambiente (usadas nos contêineres), com padrão localhost:5000.

    Uso: python -m cliente.passageiro [host] [porta]
    """
    host = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VAIJUNTO_HOST", "localhost")
    porta = int(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("VAIJUNTO_PORTA", "5000"))
    return host, porta


def autenticar(conexao: Conexao) -> str:
    """Menu de registro/login; retorna o nome do usuário autenticado."""
    while True:
        print("\n--- Acesso ---")
        print("1. Entrar")
        print("2. Registrar novo usuário")
        print("0. Sair")
        opcao = entrada("> ").strip()
        if opcao == "0":
            sys.exit(0)
        if opcao not in ("1", "2"):
            continue
        usuario = entrada("Usuário: ").strip()
        senha = entrada("Senha: ").strip()
        try:
            if opcao == "2":
                conexao.pedir("registrar", usuario=usuario, senha=senha)
                print("Usuário registrado; entrando...")
            dados = conexao.pedir("login", usuario=usuario, senha=senha)
            print(f"Bem-vindo(a), {dados['usuario']}!")
            return dados["usuario"]
        except ErroServidor as exc:
            print(f"[erro] {exc}")


def ler_inteiro(rotulo: str) -> int:
    while True:
        try:
            return int(entrada(rotulo).strip())
        except ValueError:
            print("Digite um número inteiro.")
