"""Apresentadores: o que o receptor faz com o conteúdo aceito (console/arquivos ou coletor para testes)."""
from __future__ import annotations

import os
import unicodedata
import webbrowser
from pathlib import Path

from ..codec import Message
from ..content import wrap_document

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
        "text/html": "html", "text/plain": "txt"}


def safe_console_text(text: str) -> str:
    """Remove caracteres de controle (inclusive sequências ESC) antes de imprimir em terminal."""
    return "".join(ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C"))


class CollectorPresenter:
    """Guarda as mensagens apresentadas (útil em testes)."""

    def __init__(self) -> None:
        self.messages: list = []

    def present(self, msg: Message) -> str:
        self.messages.append(msg)
        return "displayed"


class ConsolePresenter:
    """Texto no console; imagens e HTML são gravados em `inbox_dir` com nome derivado do id (nunca do remetente)."""

    def __init__(self, inbox_dir: str | Path = "inbox", open_html: bool = False, out=print):
        self.inbox_dir, self.open_html, self.out = Path(inbox_dir), open_html, out

    def _save(self, msg: Message, data: bytes) -> Path:
        os.makedirs(self.inbox_dir, mode=0o700, exist_ok=True)
        path = self.inbox_dir / f"{msg.id}.{_EXT[msg.mime]}"
        path.write_bytes(data)
        return path

    def present(self, msg: Message) -> str:
        head = f"[{msg.kind}] de {msg.sender} · {msg.id}" + (f" · adaptado: {', '.join(msg.adapted)}" if msg.adapted else "")
        if msg.kind == "text":
            self.out(head)
            self.out("    " + safe_console_text(msg.data.decode("utf-8")).replace("\n", "\n    "))
        elif msg.kind == "image":
            self.out(f"{head}\n    imagem salva em {self._save(msg, msg.data)}")
        else:
            path = self._save(msg, wrap_document(msg.data.decode("utf-8"), "OmniLink").encode("utf-8"))
            self.out(f"{head}\n    HTML (sanitizado) salvo em {path}")
            if self.open_html:
                webbrowser.open(path.resolve().as_uri())
        return "displayed"
