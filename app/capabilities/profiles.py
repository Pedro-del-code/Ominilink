"""Perfis de capacidades prontos para clientes de teste, exemplos e demonstrações de incompatibilidade."""
from __future__ import annotations

from .models import IMAGE_MIMES, ContentCap, DeviceCapabilities


def _full_content() -> dict[str, ContentCap]:
    return {
        "text": ContentCap(max_bytes=80_000, mimes=("text/plain",), max_chars=20_000),
        "html": ContentCap(max_bytes=200_000, mimes=("text/html",)),
        "image": ContentCap(max_bytes=2_000_000, mimes=IMAGE_MIMES, max_width=4096, max_height=4096),
    }


def python_receiver(name: str = "python-receiver") -> DeviceCapabilities:
    """Receptor Python completo: SSE ou long-poll, codec binário e JSON."""
    return DeviceCapabilities(name=name, platform="python", roles=("receiver",),
                              transports=("sse", "http-longpoll"), codecs=("binary-frame", "json-b64"),
                              content=_full_content(), features=("html-sandbox", "ack-displayed"))


def python_sender(name: str = "python-sender") -> DeviceCapabilities:
    return DeviceCapabilities(name=name, platform="python", roles=("sender",),
                              transports=("http-rest",), codecs=("json-b64",), content={}, features=())


def browser_receiver(name: str = "browser-receiver", *, streaming: bool = True) -> DeviceCapabilities:
    """Navegador: só json-b64; usa SSE (fetch streaming) quando disponível, senão long-poll."""
    return DeviceCapabilities(name=name, platform="browser", roles=("receiver",),
                              transports=("sse", "http-longpoll") if streaming else ("http-longpoll",),
                              codecs=("json-b64",), content=_full_content(),
                              features=("html-sandbox", "ack-displayed"))


def constrained_receiver(name: str = "constrained-receiver") -> DeviceCapabilities:
    """Dispositivo limitado (ex.: painel simples): só long-poll, JSON, texto e JPEG pequeno."""
    return DeviceCapabilities(name=name, platform="embedded", roles=("receiver",),
                              transports=("http-longpoll",), codecs=("json-b64",),
                              content={"text": ContentCap(max_bytes=4_000, mimes=("text/plain",), max_chars=1_000),
                                       "image": ContentCap(max_bytes=60_000, mimes=("image/jpeg",),
                                                           max_width=160, max_height=120)},
                              features=())


def legacy_receiver(name: str = "legacy-receiver") -> DeviceCapabilities:
    """Dispositivo que só fala um protocolo que este coordenador não implementa (demonstra diagnóstico)."""
    return DeviceCapabilities(name=name, platform="legacy", roles=("receiver",), protocol_versions=(2,),
                              transports=("ws", "lan-direct"), codecs=("cbor",),
                              content={"text": ContentCap(max_bytes=1_000, mimes=("text/plain",))}, features=())


PROFILES = {
    "python-receiver": python_receiver,
    "python-sender": python_sender,
    "browser-receiver": browser_receiver,
    "constrained-receiver": constrained_receiver,
    "legacy-receiver": legacy_receiver,
}
