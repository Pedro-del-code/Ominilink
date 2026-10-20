"""Registro de transportes e codecs conhecidos. Novos protocolos entram aqui sem alterar o motor."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransportSpec:
    name: str
    score: int                      # preferência (0-100): latência / interatividade
    supports_binary: bool = False   # permite codecs binários?
    secure_capable: bool = True     # pode operar sobre TLS?
    implemented: bool = True        # implementado por este coordenador?
    needs_sender_support: bool = False  # exige que o emissor também fale o transporte (p.ex. conexão direta)
    description: str = ""


@dataclass(frozen=True)
class CodecSpec:
    name: str
    score: int                      # preferência (0-100): eficiência de bytes
    binary: bool = False            # gera frames binários?
    description: str = ""


class ProtocolRegistry:
    def __init__(self) -> None:
        self._transports: dict[str, TransportSpec] = {}
        self._codecs: dict[str, CodecSpec] = {}

    def register_transport(self, spec: TransportSpec) -> None:
        self._transports[spec.name] = spec

    def register_codec(self, spec: CodecSpec) -> None:
        self._codecs[spec.name] = spec

    def transport(self, name: str) -> TransportSpec | None:
        return self._transports.get(name)

    def codec(self, name: str) -> CodecSpec | None:
        return self._codecs.get(name)

    def implemented_transports(self) -> list[str]:
        return sorted(n for n, s in self._transports.items() if s.implemented)

    def implemented_codecs(self) -> list[str]:
        return sorted(self._codecs)


def default_registry() -> ProtocolRegistry:
    reg = ProtocolRegistry()
    reg.register_transport(TransportSpec(
        "sse", score=80, supports_binary=False,
        description="Server-Sent Events: fluxo HTTP unidirecional servidor→receptor (push); acks via POST."))
    reg.register_transport(TransportSpec(
        "http-longpoll", score=50, supports_binary=True,
        description="Receptor consulta GET /v1/inbox com espera; frames binários obtidos por download dedicado."))
    # Conhecidos, porém NÃO implementados nesta versão (servem para diagnóstico honesto):
    reg.register_transport(TransportSpec(
        "ws", score=90, supports_binary=True, implemented=False,
        description="WebSocket (RFC 6455): requer servidor ASGI/biblioteca WS; previsto como plugin."))
    reg.register_transport(TransportSpec(
        "lan-direct", score=95, supports_binary=True, implemented=False, needs_sender_support=True,
        description="Envio direto na LAN ao receptor local; requer pareamento/autenticação mútua."))
    reg.register_codec(CodecSpec("json-b64", score=50, binary=False,
                                 description="JSON UTF-8; conteúdo binário em Base64 (+33% de bytes)."))
    reg.register_codec(CodecSpec("binary-frame", score=80, binary=True,
                                 description="Frame binário 'OLNK': cabeçalho JSON + payload bruto, sem Base64."))
    return reg
