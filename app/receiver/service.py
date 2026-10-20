"""Receptor: valida e interpreta mensagens autorizadas e decide como confirmar o recebimento.

Princípio: o receptor NÃO confia no relé — revalida tamanho, tipo e imagem e sanitiza HTML novamente.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from ..capabilities.models import DeviceCapabilities
from ..codec import Message
from ..content import sanitize_html, verify_for_receiver
from ..errors import OmniLinkError


@dataclass
class AckDecision:
    status: str                 # displayed | delivered | rejected
    reason: str | None = None


@dataclass
class GrantPolicy:
    """Quem pode enviar para este receptor. Sem política automática, o pedido fica pendente."""
    auto_accept_all: bool = False
    allowed_senders: frozenset = frozenset()
    ask: Callable[[dict], str | None] | None = None   # consulta interativa: 'approve' | 'deny' | None

    def decide(self, request: dict) -> str | None:
        if self.auto_accept_all or request.get("from") in self.allowed_senders:
            return "approve"
        return self.ask(request) if self.ask else None


@dataclass
class ReceiverService:
    device_id: str
    caps: DeviceCapabilities
    presenter: object
    grant_policy: GrantPolicy = field(default_factory=GrantPolicy)
    max_pixels: int = 8_000_000
    seen_limit: int = 2000
    _seen: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)

    def handle(self, msg: Message) -> AckDecision:
        if msg.id in self._seen:  # reentrega (ao-menos-uma-vez): repete a confirmação sem reapresentar
            return self._seen[msg.id]
        decision = self._process(msg)
        self._seen[msg.id] = decision
        while len(self._seen) > self.seen_limit:
            self._seen.popitem(last=False)
        return decision

    def _process(self, msg: Message) -> AckDecision:
        if msg.recipient != self.device_id:
            return AckDecision("rejected", "RECIPIENT_MISMATCH")
        try:
            if msg.kind == "html":
                clean, _ = sanitize_html(msg.data.decode("utf-8"))
                msg.data = clean.encode("utf-8")
            verify_for_receiver(msg, self.caps, max_pixels=self.max_pixels)
        except OmniLinkError as exc:
            return AckDecision("rejected", exc.code)
        try:
            return AckDecision(self.presenter.present(msg) or "displayed")
        except Exception as exc:  # falha ao apresentar não pode derrubar o laço de recepção
            return AckDecision("rejected", f"PRESENTATION_FAILED:{type(exc).__name__}")

    def decide_grant(self, request: dict) -> str | None:
        return self.grant_policy.decide(request)
