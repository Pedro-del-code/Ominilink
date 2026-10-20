"""Coordenador (cloud_coordinator): dispositivos, autorização, negociação, fila de entrega e confirmações.

Independente de framework web: a camada HTTP (app/api) apenas traduz requisições para chamadas daqui.
Estado compartilhado fica no Store; sessões ativas e sinais de despertar ficam em memória (instância única).
"""
from __future__ import annotations

import base64
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from ..capabilities.models import KIND_MIMES, KINDS, ContentDescriptor, DeviceCapabilities
from ..codec import Message, get_codec
from ..codec.message import new_message_id
from ..content import apply_adaptations, prepare_incoming, verify_for_receiver
from ..diagnostics import AuditLog, make
from ..errors import AuthError, Forbidden, NotFound, OmniLinkError, Unprocessable, ValidationFailure
from ..protocols import NegotiationContext, default_registry, negotiate
from ..security.tokens import issue_token, new_device_id, parse_token, verify_secret
from ..security.validation import (CLIENT_ID_RE, bounded_int, check_device_id, check_message_id, check_request_id,
                                   clean_filename, clean_label)
from .store import Store

log = logging.getLogger("omnilink.coordinator")

TERMINAL = frozenset({"delivered", "displayed", "rejected", "failed", "expired"})
ACK_STATUSES = frozenset({"delivered", "displayed", "rejected"})
GRANT_REQUEST_TTL = 3600
GRANT_RENOTIFY = 60
MAX_PENDING_GRANT_REQUESTS = 20


@dataclass
class Device:
    id: str
    name: str
    caps: DeviceCapabilities


class Incompatible(Unprocessable):
    """Negociação sem solução; carrega o resultado completo (diagnósticos e alternativas)."""

    def __init__(self, result):
        first = next((d for d in result.diagnostics if d.severity == "error"), None)
        super().__init__(first.code if first else "NO_COMMON_TRANSPORT", message=first.message if first else None)
        self.negotiation = result


class Coordinator:
    def __init__(self, settings, store: Store, registry=None, audit: AuditLog | None = None, clock=time.time):
        self.settings = settings
        self.store = store
        self.registry = registry or default_registry()
        self.audit = audit or AuditLog()
        self.clock = clock
        self.limits = settings.content_limits()
        self._lock = threading.RLock()
        self._cond = threading.Condition()
        self._versions: dict = {}
        self._alock = threading.Lock()
        self._active: dict = {}
        self._last_purge = 0.0

    # ------------------------------------------------------------------ sinais de despertar
    def _signal(self, key: str) -> None:
        with self._cond:
            if len(self._versions) > 5000:
                for stale in [k for k in self._versions if k.startswith("msg:")]:
                    del self._versions[stale]
            self._versions[key] = self._versions.get(key, 0) + 1
            self._cond.notify_all()

    def version(self, key: str) -> int:
        with self._cond:
            return self._versions.get(key, 0)

    def wait_change(self, key: str, last: int, timeout: float) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._versions.get(key, 0) != last, timeout=max(0.0, timeout))

    def mark_active(self, device_id: str, channel: str, ttl: float) -> None:
        with self._alock:
            self._active[(device_id, channel)] = time.monotonic() + ttl

    def active_channels(self, device_id: str) -> set:
        now = time.monotonic()
        with self._alock:
            for key in [k for k, exp in self._active.items() if exp <= now]:
                del self._active[key]
            return {ch for (dev, ch) in self._active if dev == device_id}

    # ------------------------------------------------------------------ dispositivos
    def register_device(self, caps_dict: dict) -> dict:
        caps = DeviceCapabilities.from_dict(caps_dict)
        with self._lock:
            active = [d for d in self.store.list("devices") if not d.get("revoked")]
            if len(active) >= self.settings.max_devices:
                raise Forbidden("DEVICE_LIMIT")
            device_id = new_device_id()
            token, token_hash = issue_token(device_id)
            now = self.clock()
            ttl = self.settings.token_ttl_days
            expires = now + ttl * 86400 if ttl else None
            self.store.put("devices", device_id, {
                "id": device_id, "name": caps.name, "token_hash": token_hash, "token_expires_at": expires,
                "caps": caps.to_dict(), "created_at": now, "last_seen": now, "revoked": False})
        self.audit.emit("device.registered", device=device_id, name=caps.name, roles=",".join(caps.roles))
        return {"device_id": device_id, "token": token, "token_expires_at": expires, "capabilities": caps.to_dict()}

    def authenticate(self, token: str) -> Device:
        device_id, secret = parse_token(token)
        rec = self.store.get("devices", device_id)
        if rec is None:
            raise AuthError("AUTH_INVALID")
        if rec.get("revoked"):
            raise AuthError("DEVICE_REVOKED")
        if not verify_secret(secret, rec["token_hash"]):
            raise AuthError("AUTH_INVALID")
        if rec.get("token_expires_at") and self.clock() > rec["token_expires_at"]:
            raise AuthError("AUTH_EXPIRED")
        if self.clock() - rec.get("last_seen", 0) > 30:
            with self._lock:  # releitura sob trava: nunca sobrescrever uma rotação de token concorrente
                fresh = self.store.get("devices", device_id)
                if fresh and not fresh.get("revoked"):
                    fresh["last_seen"] = self.clock()
                    self.store.put("devices", device_id, fresh)
        return Device(device_id, rec["name"], DeviceCapabilities.from_dict(rec["caps"]))

    def _require_active(self, device_id: str) -> dict:
        rec = self.store.get("devices", device_id)
        if rec is None:
            raise AuthError("AUTH_INVALID")
        if rec.get("revoked"):
            raise AuthError("DEVICE_REVOKED")
        return rec

    def _receiver_record(self, device_id: str) -> dict:
        rec = self.store.get("devices", device_id)
        if rec is None or rec.get("revoked"):
            raise NotFound("RECEIVER_UNKNOWN", device_id=device_id)
        return rec

    def revoke_device(self, device_id: str, *, by: str) -> None:
        with self._lock:
            rec = self.store.get("devices", device_id)
            if rec is None:
                raise NotFound("RECEIVER_UNKNOWN", device_id=device_id)
            self.store.put("devices", device_id, {"id": device_id, "name": rec.get("name", ""), "revoked": True,
                                                  "revoked_at": self.clock()})
            for coll in ("grants", "grant_requests"):
                for item in self.store.list(coll):
                    if device_id in (item.get("sender"), item.get("receiver")):
                        self.store.delete(coll, item["id"] if coll == "grant_requests" else f"{item['receiver']}:{item['sender']}")
            for msg in self.store.list("messages", owner=device_id):
                self.store.delete("messages", msg["id"])
        self._signal(f"inbox:{device_id}")
        self.audit.emit("device.revoked", device=device_id, by=by)

    def rotate_token(self, device: Device) -> dict:
        with self._lock:
            rec = self._require_active(device.id)
            token, token_hash = issue_token(device.id)
            ttl = self.settings.token_ttl_days
            rec["token_hash"], rec["token_expires_at"] = token_hash, (self.clock() + ttl * 86400 if ttl else None)
            self.store.put("devices", device.id, rec)
        self.audit.emit("device.token_rotated", device=device.id)
        return {"device_id": device.id, "token": token, "token_expires_at": rec["token_expires_at"]}

    def update_capabilities(self, device: Device, caps_dict: dict) -> dict:
        caps = DeviceCapabilities.from_dict(caps_dict)
        with self._lock:
            rec = self._require_active(device.id)
            rec["caps"], rec["name"] = caps.to_dict(), caps.name
            self.store.put("devices", device.id, rec)
        self.audit.emit("device.capabilities_updated", device=device.id)
        return caps.to_dict()

    def list_devices(self) -> list:
        out = []
        for rec in sorted(self.store.list("devices"), key=lambda r: r.get("created_at", 0)):
            if rec.get("revoked"):
                continue
            caps = rec["caps"]
            out.append({"id": rec["id"], "name": rec["name"], "platform": caps.get("platform"), "roles": caps["roles"],
                        "transports": caps["transports"], "codecs": caps["codecs"],
                        "online": bool(self.active_channels(rec["id"])), "last_seen": rec.get("last_seen")})
        return out

    def get_capabilities(self, device_id: str) -> dict:
        return self._receiver_record(check_device_id(device_id))["caps"]

    # ------------------------------------------------------------------ autorização (grants)
    def _grant_key(self, receiver_id: str, sender_id: str) -> str:
        return f"{receiver_id}:{sender_id}"

    def _require_grant(self, sender: Device, receiver_id: str, kind: str) -> None:
        grant = self.store.get("grants", self._grant_key(receiver_id, sender.id))
        if grant and kind in grant["kinds"]:
            return
        now = self.clock()
        with self._lock:
            mine = [r for r in self.store.list("grant_requests", owner=receiver_id) if r["sender"] == sender.id]
            latest = max(mine, key=lambda r: r["created_at"]) if mine else None
            if latest and latest["status"] == "denied":
                raise Forbidden("NOT_AUTHORIZED", kind=kind, request_id=latest["id"])
            if latest and latest["status"] == "pending":
                if kind not in latest["kinds"]:
                    latest["kinds"] = sorted(set(latest["kinds"]) | {kind})
                    latest["notified_at"] = None
                    self.store.put("grant_requests", latest["id"], latest, owner=receiver_id,
                                   expires_at=latest["created_at"] + GRANT_REQUEST_TTL)
                request = latest
            else:
                pending = [r for r in self.store.list("grant_requests", owner=receiver_id) if r["status"] == "pending"]
                if len(pending) >= MAX_PENDING_GRANT_REQUESTS:
                    raise Forbidden("NOT_AUTHORIZED", kind=kind, request_id="grq_" + "0" * 16)
                request = {"id": "grq_" + secrets.token_hex(8), "sender": sender.id, "sender_name": sender.name,
                           "receiver": receiver_id, "kinds": [kind], "status": "pending", "created_at": now,
                           "notified_at": None}
                self.store.put("grant_requests", request["id"], request, owner=receiver_id,
                               expires_at=now + GRANT_REQUEST_TTL)
                self.audit.emit("grant.requested", sender=sender.id, receiver=receiver_id, kind=kind)
        self._signal(f"inbox:{receiver_id}")
        raise Forbidden("NOT_AUTHORIZED", kind=kind, request_id=request["id"])

    def _store_grant(self, receiver_id: str, sender_id: str, kinds: list, by: str) -> dict:
        grant = {"receiver": receiver_id, "sender": sender_id, "kinds": sorted(set(kinds)),
                 "created_at": self.clock(), "by": by}
        self.store.put("grants", self._grant_key(receiver_id, sender_id), grant, owner=receiver_id)
        return grant

    @staticmethod
    def _check_kinds(kinds) -> list:
        if not isinstance(kinds, list) or not kinds or any(k not in KINDS for k in kinds):
            raise ValidationFailure("INVALID_REQUEST", reason="Campo 'kinds' deve listar 'text', 'image' e/ou 'html'.")
        return sorted(set(kinds))

    def decide_grant(self, receiver: Device, request_id: str, decision: str, kinds=None) -> dict:
        check_request_id(request_id)
        if decision not in ("approve", "deny"):
            raise ValidationFailure("INVALID_REQUEST", reason="Campo 'decision' deve ser 'approve' ou 'deny'.")
        with self._lock:
            req = self.store.get("grant_requests", request_id)
            if req is None or req["receiver"] != receiver.id:
                raise NotFound("NOT_FOUND")
            if req["status"] != "pending":
                return {"request_id": request_id, "status": req["status"]}
            if decision == "approve":
                allowed = self._check_kinds(kinds) if kinds is not None else req["kinds"]
                self._store_grant(receiver.id, req["sender"], allowed, by="receiver-decision")
                req["status"] = "approved"
            else:
                req["status"] = "denied"
            self.store.put("grant_requests", request_id, req, owner=receiver.id,
                           expires_at=req["created_at"] + GRANT_REQUEST_TTL)
        self.audit.emit("grant.decided", receiver=receiver.id, sender=req["sender"], decision=decision)
        return {"request_id": request_id, "status": req["status"]}

    def put_grant(self, receiver: Device, sender_id: str, kinds) -> dict:
        check_device_id(sender_id, "sender_id")
        allowed = self._check_kinds(kinds)
        self._receiver_record(sender_id)
        with self._lock:
            grant = self._store_grant(receiver.id, sender_id, allowed, by="receiver-explicit")
            for req in self.store.list("grant_requests", owner=receiver.id):
                if req["sender"] == sender_id and req["status"] != "approved":
                    req["status"] = "approved"
                    self.store.put("grant_requests", req["id"], req, owner=receiver.id,
                                   expires_at=req["created_at"] + GRANT_REQUEST_TTL)
        self.audit.emit("grant.created", receiver=receiver.id, sender=sender_id, kinds=",".join(allowed))
        return grant

    def revoke_grant(self, receiver: Device, sender_id: str) -> bool:
        check_device_id(sender_id, "sender_id")
        with self._lock:
            removed = self.store.delete("grants", self._grant_key(receiver.id, sender_id))
            for req in self.store.list("grant_requests", owner=receiver.id):
                if req["sender"] == sender_id:
                    self.store.delete("grant_requests", req["id"])
        self.audit.emit("grant.revoked", receiver=receiver.id, sender=sender_id)
        return removed

    def list_grants(self, receiver: Device) -> dict:
        grants = [{"sender": g["sender"], "kinds": g["kinds"], "created_at": g["created_at"]}
                  for g in self.store.list("grants", owner=receiver.id)]
        requests = [{"id": r["id"], "sender": r["sender"], "sender_name": r.get("sender_name"), "kinds": r["kinds"],
                     "created_at": r["created_at"]}
                    for r in self.store.list("grant_requests", owner=receiver.id) if r["status"] == "pending"]
        return {"grants": grants, "requests": requests}

    # ------------------------------------------------------------------ negociação e envio
    def _context(self, receiver_id: str, secure: bool, allow: bool) -> NegotiationContext:
        return NegotiationContext(active_transports=frozenset(self.active_channels(receiver_id)),
                                  channel_secure=secure, require_secure=self.settings.require_https,
                                  allow_adaptation=allow)

    def negotiate_preview(self, sender: Device, req: dict, *, secure: bool = True) -> dict:
        """Simulação sem envio: mostra o que seria negociado com o receptor para um conteúdo descrito."""
        to = check_device_id(req.get("to"), "to")
        kind = req.get("kind")
        if kind not in KINDS:
            raise ValidationFailure("INVALID_REQUEST", reason="Campo 'kind' deve ser 'text', 'image' ou 'html'.")
        rec = self._receiver_record(to)
        opt = {k: bounded_int(req[k], k, 0, 10**9) for k in ("size", "width", "height", "chars") if req.get(k) is not None}
        desc = ContentDescriptor(kind, req.get("mime") or KIND_MIMES[kind][0], opt.get("size", 0),
                                 opt.get("width"), opt.get("height"), opt.get("chars"))
        allow = req.get("allow_adaptation", True)
        if not isinstance(allow, bool):
            raise ValidationFailure("INVALID_REQUEST", reason="Campo 'allow_adaptation' deve ser booleano.")
        result = negotiate(sender.caps, DeviceCapabilities.from_dict(rec["caps"]), desc,
                           self._context(to, secure, allow), self.registry)
        return result.to_dict()

    def send(self, sender: Device, req: dict, *, secure: bool = True) -> dict:
        to = check_device_id(req.get("to"), "to")
        client_id = req.get("client_id")
        if client_id is not None:
            if not isinstance(client_id, str) or not CLIENT_ID_RE.match(client_id):
                raise ValidationFailure("INVALID_REQUEST", reason="Campo 'client_id' inválido.")
            prior = self.store.get("idem", f"{sender.id}:{client_id}")
            if prior:
                existing = self.store.get("messages", prior["message_id"])
                return {"ok": True, "duplicate": True, "message_id": prior["message_id"],
                        "status": existing["status"] if existing else "queued"}
        allow = req.get("allow_adaptation", True)
        if not isinstance(allow, bool):
            raise ValidationFailure("INVALID_REQUEST", reason="Campo 'allow_adaptation' deve ser booleano.")
        rec = self._receiver_record(to)
        prepared = prepare_incoming(req.get("kind"), encoding=req.get("encoding"), body=req.get("body"),
                                    mime=req.get("mime"), limits=self.limits)
        self._require_grant(sender, to, prepared.kind)
        receiver_caps = DeviceCapabilities.from_dict(rec["caps"])
        result = negotiate(sender.caps, receiver_caps, prepared.descriptor, self._context(to, secure, allow), self.registry)
        if not result.compatible:
            self.audit.emit("message.incompatible", sender=sender.id, receiver=to, kind=prepared.kind)
            raise Incompatible(result)
        msg = Message(new_message_id(), sender.id, to, prepared.kind, prepared.mime, prepared.data,
                      filename=clean_filename(req.get("filename")), adapted=list(prepared.notes))
        apply_adaptations(msg, result.selection.adaptations, self.limits)
        verify_for_receiver(msg, receiver_caps, max_pixels=self.limits.max_image_pixels)
        now = self.clock()
        ttl = self.settings.queue_ttl_seconds
        with self._lock:
            pending = [m for m in self.store.list("messages", owner=to) if m["status"] in ("queued", "sent")]
            if len(pending) >= self.settings.queue_max_per_device:
                raise OmniLinkError("QUEUE_FULL", status=429, limit=self.settings.queue_max_per_device)
            record = {"id": msg.id, "sender": sender.id, "recipient": to, "kind": msg.kind, "mime": msg.mime,
                      "filename": msg.filename, "size": len(msg.data), "sha256": msg.sha256, "adapted": msg.adapted,
                      "plan": result.selection.to_dict(), "data_b64": base64.b64encode(msg.data).decode("ascii"),
                      "status": "queued", "reason": None, "attempts": 0, "created_at": now, "updated_at": now,
                      "sent_at": 0, "delivered_via": None, "content_expires_at": now + ttl,
                      "purge_at": now + ttl + self.settings.status_retention_seconds}
            self.store.put("messages", msg.id, record, owner=to, expires_at=record["purge_at"])
            if client_id:
                self.store.put("idem", f"{sender.id}:{client_id}", {"message_id": msg.id}, owner=sender.id,
                               expires_at=now + ttl)
        self._signal(f"inbox:{to}")
        # o detalhe das combinações descartadas continua em negotiation.considered; aqui só o que importa ao emissor
        diagnostics = [d for d in result.diagnostics if d.severity != "error" and d.code != "CODEC_NEEDS_BINARY_TRANSPORT"]
        if not self.active_channels(to):
            diagnostics.append(make("RECEIVER_OFFLINE", ttl=ttl))
        self.audit.emit("message.accepted", message=msg.id, sender=sender.id, receiver=to, kind=msg.kind,
                        size=len(msg.data), transport=result.selection.transport, codec=result.selection.codec)
        return {"ok": True, "message_id": msg.id, "status": "queued", "adapted": msg.adapted,
                "negotiation": {"selection": result.selection.to_dict(), "considered": result.considered},
                "diagnostics": [d.to_dict() for d in diagnostics]}

    # ------------------------------------------------------------------ entrega ao receptor
    def _message_from_record(self, r: dict) -> Message:
        return Message(id=r["id"], sender=r["sender"], recipient=r["recipient"], kind=r["kind"], mime=r["mime"],
                       data=base64.b64decode(r["data_b64"]), filename=r.get("filename"), created_at=r["created_at"],
                       sha256=r["sha256"], adapted=list(r["adapted"]))

    def _put_message(self, r: dict) -> None:
        self.store.put("messages", r["id"], r, owner=r["recipient"], expires_at=r["purge_at"])

    def _finish(self, r: dict, status: str, reason: str | None = None) -> None:
        r["status"], r["reason"], r["updated_at"] = status, reason, self.clock()
        if status in TERMINAL:
            r["data_b64"] = None  # o conteúdo não é retido depois da entrega
        self._put_message(r)
        self._signal(f"msg:{r['id']}")

    def collect_inbox(self, device: Device, *, limit: int = 10, channel: str = "http-longpoll",
                      resend_all: bool = False) -> list:
        """Itens prontos para entrega: ('message', Message) e ('grant.request', dict). Marca mensagens como enviadas."""
        self._require_active(device.id)
        now, items = self.clock(), []
        with self._lock:
            for r in sorted(self.store.list("messages", owner=device.id), key=lambda x: x["created_at"]):
                if len(items) >= limit:
                    break
                if r["status"] not in ("queued", "sent"):
                    continue
                if now > r["content_expires_at"]:
                    self._finish(r, "expired", "tempo de vida na fila esgotado")
                    continue
                if r["status"] == "sent" and not resend_all and now - r["sent_at"] < self.settings.redeliver_after_seconds:
                    continue
                if r["attempts"] >= self.settings.max_delivery_attempts:
                    self._finish(r, "failed", "DELIVERY_ATTEMPTS_EXCEEDED")
                    continue
                r["status"], r["attempts"], r["sent_at"] = "sent", r["attempts"] + 1, now
                r["delivered_via"], r["updated_at"] = channel, now
                self._put_message(r)
                items.append(("message", self._message_from_record(r)))
            for g in self.store.list("grant_requests", owner=device.id):
                if len(items) >= limit or g["status"] != "pending":
                    continue
                if resend_all or g.get("notified_at") is None or now - g["notified_at"] > GRANT_RENOTIFY:
                    g["notified_at"] = now
                    self.store.put("grant_requests", g["id"], g, owner=device.id,
                                   expires_at=g["created_at"] + GRANT_REQUEST_TTL)
                    items.append(("grant.request", {"type": "grant.request", "id": g["id"], "from": g["sender"],
                                                    "from_name": g.get("sender_name"), "kinds": g["kinds"],
                                                    "created_at": g["created_at"]}))
        return items

    def fetch_inbox(self, device: Device, *, wait: float, limit: int, channel: str, resend_all: bool = False) -> list:
        wait = max(0.0, min(float(wait), float(self.settings.longpoll_max_wait)))
        deadline, key, first = time.monotonic() + wait, f"inbox:{device.id}", True
        while True:
            self.mark_active(device.id, channel, wait + 10)
            version = self.version(key)
            items = self.collect_inbox(device, limit=limit, channel=channel, resend_all=resend_all and first)
            first = False
            if items:
                return items
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            self.wait_change(key, version, min(remaining, 1.0))  # 1 s: também funciona com Store compartilhado

    def message_frame(self, device: Device, message_id: str) -> bytes:
        check_message_id(message_id)
        r = self.store.get("messages", message_id)
        if r is None or r["recipient"] != device.id or not r.get("data_b64"):
            raise NotFound("NOT_FOUND")
        return get_codec("binary-frame").encode(self._message_from_record(r))

    def ack(self, device: Device, message_id: str, status: str, reason: str | None = None) -> dict:
        check_message_id(message_id)
        if status not in ACK_STATUSES:
            raise ValidationFailure("INVALID_REQUEST", reason="Campo 'status' deve ser delivered, displayed ou rejected.")
        reason = clean_label(reason, "reason", 200) if reason else None
        with self._lock:
            r = self.store.get("messages", message_id)
            if r is None or r["recipient"] != device.id:
                raise NotFound("NOT_FOUND")
            upgrade = r["status"] == "delivered" and status == "displayed"
            if r["status"] in TERMINAL and not upgrade:
                return self._status_view(r)
            self._finish(r, status, reason)
        self.audit.emit("message.status", message=message_id, status=status, device=device.id)
        return self._status_view(r)

    @staticmethod
    def _status_view(r: dict) -> dict:
        return {"id": r["id"], "status": r["status"], "reason": r.get("reason"), "from": r["sender"], "to": r["recipient"],
                "kind": r["kind"], "size": r["size"], "adapted": r["adapted"], "plan": r.get("plan"),
                "delivered_via": r.get("delivered_via"), "attempts": r["attempts"],
                "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def message_status(self, requester: Device, message_id: str, *, wait: float = 0.0) -> dict:
        check_message_id(message_id)
        deadline = time.monotonic() + max(0.0, min(float(wait), float(self.settings.longpoll_max_wait)))
        key = f"msg:{message_id}"
        while True:
            version = self.version(key)
            r = self.store.get("messages", message_id)
            if r is None or requester.id not in (r["sender"], r["recipient"]):
                raise NotFound("NOT_FOUND")
            if r["status"] not in TERMINAL and self.clock() > r["content_expires_at"]:
                with self._lock:
                    self._finish(r, "expired", "tempo de vida na fila esgotado")
            view = self._status_view(r)
            remaining = deadline - time.monotonic()
            if view["status"] in TERMINAL or remaining <= 0:
                return view
            self.wait_change(key, version, min(remaining, 1.0))

    # ------------------------------------------------------------------ manutenção
    def maybe_purge(self) -> None:
        now = time.monotonic()
        if now - self._last_purge >= 30:
            self._last_purge = now
            try:
                self.store.purge_expired()
            except Exception:  # manutenção nunca deve derrubar uma requisição
                log.exception("falha ao limpar registros expirados")
