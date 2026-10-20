"""Transporte 'http-longpoll': o receptor consulta GET /v1/inbox com espera; frames binários por download."""
from __future__ import annotations

import json
import threading
from typing import Iterator

from ..codec import get_codec
from ..errors import OmniLinkError
from .base import ReceiverTransport


class LongPollTransport(ReceiverTransport):
    name = "http-longpoll"
    wait_seconds = 20

    def events(self, stop: threading.Event) -> Iterator[tuple]:
        from ..client.api import ApiError  # import tardio: evita ciclo client <-> transport
        first = True
        while not stop.is_set():
            # primeira consulta imediata (wait=0): confirma a conexão sem esperar; as seguintes aguardam
            data = self.client.fetch_inbox(wait=0 if first else self.wait_seconds, limit=10, codec=self.codec)
            if first:
                first = False
                yield "ready", {"transport": self.name, "codec": self.codec}
            for item in data.get("items", []):
                if item.get("type") == "grant.request":
                    yield "grant.request", item
                    continue
                mid = item.get("id")
                try:
                    if item.get("encoding") == "binary":
                        msg = get_codec("binary-frame").decode(self.client.fetch_frame(mid), max_bytes=self.max_bytes)
                    else:
                        msg = get_codec("json-b64").decode(json.dumps(item), max_bytes=self.max_bytes)
                    yield "message", msg
                except OmniLinkError as exc:
                    yield "invalid", {"id": mid, "code": exc.code, "message": exc.diagnostic.message}
                except ApiError as exc:
                    if exc.status != 404:  # 404: a mensagem expirou entre a listagem e o download
                        raise
