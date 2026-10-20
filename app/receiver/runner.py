"""Laço de recepção: conecta pelo transporte escolhido, trata eventos e reconecta com tentativas limitadas."""
from __future__ import annotations

import logging
import threading
import time

import requests

from ..client.api import ApiError, OmniLinkClient
from ..client.retry import ReconnectLimitExceeded, RetryPolicy
from ..transport import create_transport
from .service import ReceiverService

log = logging.getLogger("omnilink.receiver")


class ReceiverRunner:
    def __init__(self, client: OmniLinkClient, service: ReceiverService, *, transport: str = "sse",
                 codec: str = "json-b64", retry: RetryPolicy | None = None, stop: threading.Event | None = None,
                 sleep=time.sleep, on_event=None):
        self.client, self.service = client, service
        self.retry = retry or client.retry
        self.stop = stop or threading.Event()
        self._sleep, self._on_event = sleep, on_event
        max_bytes = max([c.max_bytes for c in service.caps.content.values()] or [2_000_000])
        self.transport = create_transport(transport, client, codec, max_bytes)
        self.connections = 0

    def _emit(self, kind: str, payload) -> None:
        if self._on_event:
            self._on_event(kind, payload)

    def _safe_ack(self, message_id: str, status: str, reason: str | None) -> None:
        try:
            self.client.ack(message_id, status, reason)
        except (ApiError, requests.RequestException) as exc:
            # sem confirmação o coordenador reentrega depois; o receptor deduplica pelo id
            log.warning("falha ao confirmar %s (%s); será reentregue", message_id, exc)

    def _handle(self, kind: str, payload) -> None:
        if kind == "message":
            decision = self.service.handle(payload)
            self._safe_ack(payload.id, decision.status, decision.reason)
        elif kind == "invalid" and payload.get("id"):
            self._safe_ack(payload["id"], "rejected", payload.get("code"))
        elif kind == "grant.request":
            decision = self.service.decide_grant(payload)
            if decision:
                try:
                    self.client.decide_grant(payload["id"], decision)
                except ApiError as exc:
                    log.warning("falha ao responder pedido de autorização: %s", exc)
            else:
                log.info("pedido de autorização %s de %s aguardando decisão manual", payload["id"], payload["from"])
        elif kind == "revoked":
            raise ApiError(401, {"error": {"code": "DEVICE_REVOKED", "message": "Dispositivo revogado."}})
        self._emit(kind, payload)

    def run(self) -> None:
        failures = 0
        while not self.stop.is_set():
            try:
                for kind, payload in self.transport.events(self.stop):
                    if kind == "ready":
                        failures, self.connections = 0, self.connections + 1  # conexão estabelecida: zera o contador
                    self._handle(kind, payload)
                self._sleep(0.2)  # fim normal da conexão (ex.: duração máxima do stream): reconecta sem penalidade
                continue
            except ApiError as exc:
                if exc.status in (401, 403):
                    raise  # credencial inválida/revogada: reconectar não resolve
                failure = exc
            except (requests.RequestException, OSError) as exc:
                failure = exc
            failures += 1
            if failures > self.retry.max_attempts:
                raise ReconnectLimitExceeded(failures - 1)
            delay = self.retry.delay(failures)
            log.warning("conexão falhou (%s); tentativa %d/%d em %.1fs", failure, failures, self.retry.max_attempts, delay)
            self._emit("reconnecting", {"attempt": failures, "delay": delay})
            self._sleep(delay)
