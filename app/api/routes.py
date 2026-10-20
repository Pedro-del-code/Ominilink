"""Rotas HTTP do coordenador (Flask). Cada rota valida, delega ao Coordinator e formata a resposta."""
from __future__ import annotations

import json
import time
from functools import wraps

from flask import Blueprint, Response, current_app, g, jsonify, request, send_from_directory

from .. import __version__
from ..codec import get_codec
from ..diagnostics import make
from ..errors import AuthError, Forbidden, NotFound, RateLimited, ValidationFailure
from ..security.tokens import check_enrollment_key
from .factory import STATIC_DIR

bp = Blueprint("api", __name__)


def _ext():
    return current_app.extensions["omnilink"]


def _co():
    return _ext().coordinator


def _json_body() -> dict:
    raw = request.get_data(cache=False)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValidationFailure("INVALID_REQUEST", reason="Corpo da requisição não é JSON válido.") from None
    if not isinstance(data, dict):
        raise ValidationFailure("INVALID_REQUEST", reason="O corpo deve ser um objeto JSON.")
    return data


def _num_arg(name: str, default: float, lo: float, hi: float) -> float:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValidationFailure("INVALID_REQUEST", reason=f"Parâmetro '{name}' deve ser numérico.") from None
    return max(lo, min(hi, value))


def _limit(limiter, key: str) -> None:
    allowed, retry = limiter.check(key)
    if not allowed:
        _co().audit.emit("rate.limited", key=key[:24], path=request.path)
        raise RateLimited("RATE_LIMITED", retry_after=retry)


def authenticated(view):
    """Exige 'Authorization: Bearer <token>'; bloqueia IPs com muitas falhas e aplica limite por dispositivo."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        ext, ip = _ext(), request.remote_addr or "?"
        blocked, retry = ext.authfail_limiter.blocked(ip)
        if blocked:
            raise RateLimited("RATE_LIMITED", retry_after=retry)
        header = request.headers.get("Authorization", "")
        if not header[:7].lower() == "bearer ":
            raise AuthError("AUTH_REQUIRED")
        try:
            g.device = ext.coordinator.authenticate(header[7:].strip())
        except AuthError as exc:
            ext.authfail_limiter.check(ip)
            ext.coordinator.audit.emit("auth.failed", ip=ip, code=exc.code)
            raise
        _limit(ext.ctl_limiter, g.device.id)
        return view(*args, **kwargs)
    return wrapper


def _require_enrollment_key() -> None:
    ext, ip = _ext(), request.remote_addr or "?"
    _limit(ext.reg_limiter, ip)
    if not check_enrollment_key(request.headers.get("X-Enrollment-Key"), ext.settings.enrollment_key):
        ext.coordinator.audit.emit("auth.enrollment_denied", ip=ip)
        raise AuthError("ENROLLMENT_DENIED")


# ---------------------------------------------------------------------------------------- público
@bp.get("/health")
def health():
    co = _co()
    ok = co.store.ping()
    body = {"status": "ok" if ok else "degraded", "service": "omnilink-coordinator", "version": __version__,
            "store": co.store.kind, "persistent": co.store.kind != "memory", "time": round(time.time(), 3)}
    return jsonify(body), (200 if ok else 503)


@bp.get("/")
def index():
    return jsonify({"name": "OmniLink coordinator", "version": __version__, "receiver_page": "/receiver",
                    "health": "/health", "api": "/v1", "docs": "docs/ARQUITETURA_E_PROTOCOLO.md"})


@bp.get("/receiver")
def receiver_page():
    resp = send_from_directory(STATIC_DIR, "receiver.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------------------- dispositivos
@bp.post("/v1/devices")
def register_device():
    _require_enrollment_key()
    caps = _json_body().get("capabilities")
    if caps is None:
        raise ValidationFailure("INVALID_REQUEST", reason="Campo 'capabilities' é obrigatório.")
    return jsonify({"ok": True, **_co().register_device(caps)}), 201


@bp.get("/v1/devices")
@authenticated
def list_devices():
    return jsonify({"ok": True, "devices": _co().list_devices()})


@bp.get("/v1/devices/<device_id>/capabilities")
@authenticated
def device_capabilities(device_id):
    return jsonify({"ok": True, "device_id": device_id, "capabilities": _co().get_capabilities(device_id)})


@bp.put("/v1/devices/me/capabilities")
@authenticated
def update_capabilities():
    body = _json_body()
    return jsonify({"ok": True, "capabilities": _co().update_capabilities(g.device, body.get("capabilities", body))})


@bp.post("/v1/devices/me/rotate-token")
@authenticated
def rotate_token():
    return jsonify({"ok": True, **_co().rotate_token(g.device)})


@bp.delete("/v1/devices/me")
@authenticated
def revoke_self():
    _co().revoke_device(g.device.id, by=g.device.id)
    return jsonify({"ok": True, "revoked": g.device.id})


@bp.delete("/v1/admin/devices/<device_id>")
def admin_revoke(device_id):
    _require_enrollment_key()
    _co().revoke_device(device_id, by="admin")
    return jsonify({"ok": True, "revoked": device_id})


@bp.get("/v1/admin/events")
def admin_events():
    _require_enrollment_key()
    return jsonify({"ok": True, "events": _co().audit.recent(int(_num_arg("limit", 100, 1, 500)))})


# ---------------------------------------------------------------------------------------- mensagens
@bp.post("/v1/negotiate")
@authenticated
def negotiate_preview():
    return jsonify({"ok": True, "negotiation": _co().negotiate_preview(g.device, _json_body(), secure=request.is_secure)})


@bp.post("/v1/messages")
@authenticated
def send_message():
    _limit(_ext().msg_limiter, g.device.id)
    result = _co().send(g.device, _json_body(), secure=request.is_secure)
    return jsonify(result), 202


@bp.get("/v1/messages/<message_id>")
@authenticated
def message_status(message_id):
    wait = _num_arg("wait", 0, 0, 60)
    return jsonify({"ok": True, **_co().message_status(g.device, message_id, wait=wait)})


@bp.get("/v1/messages/<message_id>/frame")
@authenticated
def message_frame(message_id):
    return Response(_co().message_frame(g.device, message_id), mimetype="application/vnd.omnilink.frame")


# ---------------------------------------------------------------------------------------- receptor
def _encode_item(kind: str, value, codec: str) -> dict:
    if kind != "message":
        return value
    if codec == "binary-frame":  # descritor + download binário dedicado (sem Base64)
        meta = value.meta()
        meta.update({"encoding": "binary", "frame": f"/v1/messages/{value.id}/frame"})
        return meta
    return get_codec("json-b64").to_dict(value)


@bp.get("/v1/inbox")
@authenticated
def inbox():
    codec = request.args.get("codec", "json-b64")
    get_codec(codec)
    items = _co().fetch_inbox(g.device, wait=_num_arg("wait", 0, 0, 60), limit=int(_num_arg("limit", 10, 1, 50)),
                              channel="http-longpoll")
    return jsonify({"ok": True, "transport": "http-longpoll", "codec": codec,
                    "items": [{"type": k, **_encode_item(k, v, codec)} if k == "message" else v for k, v in items]})


def _sse(event: str, data: dict, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@bp.get("/v1/stream")
@authenticated
def stream():
    co, settings, device = _co(), _ext().settings, g.device
    codec = get_codec("json-b64")

    def generate():
        started = last_sent = time.monotonic()
        first, key = True, f"inbox:{device.id}"
        yield _sse("ready", {"device_id": device.id, "transport": "sse", "codec": "json-b64",
                             "server_time": round(time.time(), 3), "max_seconds": settings.sse_max_seconds})
        try:
            while time.monotonic() - started < settings.sse_max_seconds:
                co.mark_active(device.id, "sse", 5)
                version = co.version(key)
                items = co.collect_inbox(device, limit=10, channel="sse", resend_all=first)
                first = False
                for kind, value in items:
                    payload = codec.to_dict(value) if kind == "message" else value
                    yield _sse(kind, payload, payload.get("id"))
                    last_sent = time.monotonic()
                if items:
                    continue
                if time.monotonic() - last_sent >= settings.sse_keepalive_seconds:
                    yield ": keepalive\n\n"
                    last_sent = time.monotonic()
                co.wait_change(key, version, 1.0)
        except AuthError as exc:
            yield _sse("revoked", {"code": exc.code})
            return
        yield _sse("bye", {"reason": "max-duration"})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@bp.post("/v1/inbox/ack")
@authenticated
def ack():
    body = _json_body()
    return jsonify({"ok": True, **_co().ack(g.device, body.get("id"), body.get("status"), body.get("reason"))})


# ---------------------------------------------------------------------------------------- autorizações
@bp.get("/v1/grants")
@authenticated
def list_grants():
    return jsonify({"ok": True, **_co().list_grants(g.device)})


@bp.put("/v1/grants/<sender_id>")
@authenticated
def put_grant(sender_id):
    return jsonify({"ok": True, "grant": _co().put_grant(g.device, sender_id, _json_body().get("kinds"))})


@bp.delete("/v1/grants/<sender_id>")
@authenticated
def revoke_grant(sender_id):
    return jsonify({"ok": True, "revoked": _co().revoke_grant(g.device, sender_id)})


@bp.post("/v1/grants/requests/<request_id>")
@authenticated
def decide_grant(request_id):
    body = _json_body()
    return jsonify({"ok": True, **_co().decide_grant(g.device, request_id, body.get("decision"), body.get("kinds"))})
