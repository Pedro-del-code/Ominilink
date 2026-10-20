"""Transportes do lado do receptor: cada um transforma uma 'conexão' em um fluxo de eventos tipados.

Eventos: ("ready", dict) · ("message", Message) · ("grant.request", dict) · ("invalid", dict) ·
         ("revoked", dict) · ("bye", dict)
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Callable, Iterator


class ReceiverTransport(ABC):
    name = "abstract"

    def __init__(self, client, codec: str = "json-b64", max_bytes: int = 2_000_000):
        self.client, self.codec, self.max_bytes = client, codec, max_bytes

    @abstractmethod
    def events(self, stop: threading.Event) -> Iterator[tuple]:
        """Abre UMA conexão e produz eventos até ela terminar. Erros de rede propagam para o chamador."""


_FACTORIES: dict[str, Callable[..., ReceiverTransport]] = {}


def register_transport(name: str, factory: Callable[..., ReceiverTransport]) -> None:
    _FACTORIES[name] = factory


def create_transport(name: str, client, codec: str = "json-b64", max_bytes: int = 2_000_000) -> ReceiverTransport:
    try:
        return _FACTORIES[name](client, codec, max_bytes)
    except KeyError:
        raise ValueError(f"Transporte de receptor desconhecido: {name!r} (disponíveis: {sorted(_FACTORIES)})") from None


def available_transports() -> list:
    return sorted(_FACTORIES)
