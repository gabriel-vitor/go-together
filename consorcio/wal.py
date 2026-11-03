"""
WAL (write-ahead log) do coordenador de transações.

Regra de ouro do 2PC: o coordenador grava a DECISÃO em disco ANTES de
comunicá-la aos participantes. Assim, se ele cair entre decidir e avisar,
a recuperação relê o log e termina o trabalho (reenvia COMMITs/ABORTs),
mantendo o consórcio consistente.

Formato: arquivo append-only com um objeto JSON por linha (JSONL), com
flush + fsync a cada registro — a linha só "existe" depois de durável.
Uma linha truncada por queda no meio da escrita é ignorada na releitura.
"""

import json
import os
import threading


class WAL:
    def __init__(self, caminho: str):
        self.caminho = caminho
        self._trava = threading.Lock()
        os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)

    def registrar(self, registro: dict) -> None:
        linha = json.dumps(registro, ensure_ascii=False)
        with self._trava:
            with open(self.caminho, "a", encoding="utf-8") as arquivo:
                arquivo.write(linha + "\n")
                arquivo.flush()
                os.fsync(arquivo.fileno())

    def ler_tudo(self) -> list[dict]:
        if not os.path.exists(self.caminho):
            return []
        registros = []
        with open(self.caminho, encoding="utf-8") as arquivo:
            for linha in arquivo:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    registros.append(json.loads(linha))
                except json.JSONDecodeError:
                    # Linha truncada por queda durante a escrita: descartada.
                    continue
        return registros
