"""Cliente HTTP do coordenador (usado pelos clientes de teste, pela CLI e pelos exemplos)."""
from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator

import requests

from ..capabilities.models import DeviceCapabilities
from .retry import RetryPolicy


class ApiError(Exception):
    """Erro devolvido pelo coordenador (preserva código de diagnóstico, status HTTP e corpo completo)."""

    def __init__(self, status: int, body: dict | None, retry_after: float | None = None):
        error = (body or {}).get("error") or {}
        self.status, self.body, self.retry_after = status, body or {}, retry_after
        self.code = error.get("code", "HTTP_%d" % status)
        self.diagnostic = error
        self.negotiation = (body or {}).get("negotiation")
        super().__init__(f"{self.code}: {error.get('message', 'erro HTTP %d' % status)}")

    @classmethod
    def from_response(cls, resp: requests.Response) -> "ApiError":
        try:
            body = resp.json()
        except ValueError:
            body = {"error": {"code": f"HTTP_{resp.status_code}", "message": resp.text[:200]}}
        retry = resp.headers.get("Retry-After")
        return cls(resp.status_code, body, float(retry) if retry and retry.replace(".", "").isdigit() else None)


class OmniLinkClient:
    def __init__(self, base_url: str, token: str | None = None, *, timeout: float = 15.0,
                 retry: RetryPolicy | None = None, session: requests.Session | None = None, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retry = retry or RetryPolicy()
        self.session = session or requests.Session()
        self._sleep = sleep

    @property
    def device_id(self) -> str | None:
        return self.token.split(".", 1)[0] if self.token else None

    # ------------------------------------------------------------------ infraestrutura HTTP
    def _request(self, method: str, path: str, *, body=None, params=None, retry: bool = False, headers=None,
                 stream: bool = False, timeout=None) -> requests.Response:
        hdrs = {"Accept": "application/json", **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
                **(headers or {})}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(method, self.base_url + path, json=body, params=params, headers=hdrs,
                                            timeout=timeout or self.timeout, stream=stream)
            except (requests.ConnectionError, requests.Timeout):
                if not retry or attempt >= self.retry.max_attempts:
                    raise
                self._sleep(self.retry.delay(attempt))
                continue
            if retry and resp.status_code in (502, 503, 504) and attempt < self.retry.max_attempts:
                resp.close()  # ex.: instância do Render ainda iniciando
                self._sleep(self.retry.delay(attempt))
                continue
            return resp

    def _call(self, method: str, path: str, **kw) -> dict:
        resp = self._request(method, path, **kw)
        if resp.status_code >= 400:
            raise ApiError.from_response(resp)
        return resp.json()

    # ------------------------------------------------------------------ dispositivos
    def health(self) -> dict:
        return self._call("GET", "/health", retry=True)

    def register(self, enrollment_key: str, capabilities) -> dict:
        caps = capabilities.to_dict() if isinstance(capabilities, DeviceCapabilities) else capabilities
        data = self._call("POST", "/v1/devices", body={"capabilities": caps},
                          headers={"X-Enrollment-Key": enrollment_key})
        self.token = data["token"]
        return data

    def devices(self) -> list:
        return self._call("GET", "/v1/devices", retry=True)["devices"]

    def capabilities(self, device_id: str) -> dict:
        return self._call("GET", f"/v1/devices/{device_id}/capabilities", retry=True)["capabilities"]

    def update_capabilities(self, capabilities) -> dict:
        caps = capabilities.to_dict() if isinstance(capabilities, DeviceCapabilities) else capabilities
        return self._call("PUT", "/v1/devices/me/capabilities", body={"capabilities": caps})["capabilities"]

    def rotate_token(self) -> dict:
        data = self._call("POST", "/v1/devices/me/rotate-token")
        self.token = data["token"]
        return data

    def revoke_self(self) -> dict:
        return self._call("DELETE", "/v1/devices/me")

    # ------------------------------------------------------------------ envio
    def negotiate(self, to: str, kind: str, **descriptor) -> dict:
        return self._call("POST", "/v1/negotiate", body={"to": to, "kind": kind, **descriptor})["negotiation"]

    def send(self, to: str, kind: str, body: str, encoding: str, *, mime=None, filename=None,
             allow_adaptation: bool = True, client_id: str | None = None) -> dict:
        payload = {"to": to, "kind": kind, "body": body, "encoding": encoding, "allow_adaptation": allow_adaptation,
                   "client_id": client_id or uuid.uuid4().hex}  # chave de idempotência: reenvios não duplicam
        if mime:
            payload["mime"] = mime
        if filename:
            payload["filename"] = filename
        return self._call("POST", "/v1/messages", body=payload, retry=True)

    def send_text(self, to: str, text: str, **kw) -> dict:
        return self.send(to, "text", text, "utf-8", **kw)

    def send_html(self, to: str, html: str, **kw) -> dict:
        return self.send(to, "html", html, "utf-8", **kw)

    def send_image(self, to: str, source, *, filename: str | None = None, **kw) -> dict:
        if isinstance(source, (str, Path)):
            path = Path(source)
            data, filename = path.read_bytes(), filename or path.name
        else:
            data = bytes(source)
        return self.send(to, "image", base64.b64encode(data).decode("ascii"), "base64", filename=filename, **kw)

    def message_status(self, message_id: str, wait: float = 0) -> dict:
        return self._call("GET", f"/v1/messages/{message_id}", params={"wait": wait}, retry=True,
                          timeout=self.timeout + wait)

    # ------------------------------------------------------------------ recepção
    def ack(self, message_id: str, status: str, reason: str | None = None) -> dict:
        return self._call("POST", "/v1/inbox/ack", body={"id": message_id, "status": status, "reason": reason},
                          retry=True)

    def fetch_inbox(self, *, wait: float = 20, limit: int = 10, codec: str = "json-b64") -> dict:
        return self._call("GET", "/v1/inbox", params={"wait": wait, "limit": limit, "codec": codec},
                          timeout=(10, wait + 15))

    def fetch_frame(self, message_id: str) -> bytes:
        resp = self._request("GET", f"/v1/messages/{message_id}/frame", headers={"Accept": "*/*"})
        if resp.status_code >= 400:
            raise ApiError.from_response(resp)
        return resp.content

    def stream_events(self, stop: threading.Event) -> Iterator[tuple]:
        """Lê o fluxo SSE e produz (evento, dados). Uma chamada = uma conexão."""
        resp = self._request("GET", "/v1/stream", stream=True, headers={"Accept": "text/event-stream"},
                             timeout=(10, 45))
        try:
            if resp.status_code != 200:
                raise ApiError.from_response(resp)
            resp.encoding = "utf-8"
            event, data = "message", []
            for line in resp.iter_lines(decode_unicode=True):
                if stop.is_set():
                    return
                if line is None or line.startswith(":"):
                    continue
                if line == "":
                    if data:
                        yield event, json.loads("\n".join(data))
                    event, data = "message", []
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
        finally:
            resp.close()

    # ------------------------------------------------------------------ autorizações
    def grants(self) -> dict:
        return self._call("GET", "/v1/grants", retry=True)

    def grant(self, sender_id: str, kinds: list) -> dict:
        return self._call("PUT", f"/v1/grants/{sender_id}", body={"kinds": kinds})["grant"]

    def revoke_grant(self, sender_id: str) -> bool:
        return self._call("DELETE", f"/v1/grants/{sender_id}")["revoked"]

    def decide_grant(self, request_id: str, decision: str, kinds: list | None = None) -> dict:
        body = {"decision": decision, **({"kinds": kinds} if kinds else {})}
        return self._call("POST", f"/v1/grants/requests/{request_id}", body=body)
