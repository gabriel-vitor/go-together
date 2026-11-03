"""
Relógio lógico de Lamport.

Os relógios físicos dos servidores do consórcio NÃO são sincronizados, e as
mensagens podem atrasar — logo, carimbos de parede não servem para decidir
quem pediu "primeiro". O relógio de Lamport dá uma ordem causal: todo evento
local incrementa o contador, e toda mensagem recebida ajusta o contador para
max(local, recebido) + 1. O par (contador, id_da_empresa) forma uma ORDEM
TOTAL entre as transações do consórcio (o id desempata contadores iguais), e
é essa ordem que define a precedência na disputa pelo último assento e
alimenta o esquema wait-die de prevenção de deadlock distribuído.
"""

import threading


class RelogioLamport:
    def __init__(self, inicial: int = 0):
        self._contador = inicial
        self._trava = threading.Lock()

    def tique(self) -> int:
        """Evento local: incrementa e devolve o novo valor."""
        with self._trava:
            self._contador += 1
            return self._contador

    def observar(self, recebido) -> int:
        """Mensagem recebida: ajusta para max(local, recebido) + 1."""
        try:
            recebido = int(recebido)
        except (TypeError, ValueError):
            return self.tique()
        with self._trava:
            self._contador = max(self._contador, recebido) + 1
            return self._contador

    @property
    def valor(self) -> int:
        with self._trava:
            return self._contador
