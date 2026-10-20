"""Transporte 'sse': push servidor→receptor sobre HTTP (Server-Sent Events); confirmações por POST."""
from __future__ import annotations

import json
import threading
from typing import Iterator

from ..codec import get_codec
from ..errors import OmniLinkError
from .base import ReceiverTransport


class SseTransport(ReceiverTransport):
    name = "sse"

    def events(self, stop: threading.Event) -> Iterator[tuple]:
        codec = get_codec("json-b64")
        for event, data in self.client.stream_events(stop):
            if event == "message":
                try:
                    yield "message", codec.decode(json.dumps(data), max_bytes=self.max_bytes)
                except OmniLinkError as exc:
                    yield "invalid", {"id": data.get("id") if isinstance(data, dict) else None,
                                      "code": exc.code, "message": exc.diagnostic.message}
            else:
                yield event, data
