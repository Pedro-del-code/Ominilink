"""Fábrica da aplicação Flask: configuração, cabeçalhos de segurança, limites e tratamento de erros."""
from __future__ import annotations

import logging
import math
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from .. import __version__
from ..config import Settings
from ..coordinator.service import Coordinator, Incompatible
from ..coordinator.store import Store, open_store
from ..diagnostics import configure_logging, make
from ..errors import OmniLinkError
from ..security.ratelimit import RateLimiter

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("omnilink.api")

# Política da página do receptor: sem scripts inline; iframes de conteúdo herdam a política (srcdoc).
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def create_app(settings: Settings | None = None, store: Store | None = None) -> Flask:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level, settings.log_json)
    if settings.ephemeral_key:
        print(f"[OmniLink] OMNILINK_ENROLLMENT_KEY não definida — chave EFÊMERA de desenvolvimento: "
              f"{settings.enrollment_key}", flush=True)
    coordinator = Coordinator(settings, store if store is not None else open_store(settings.database_url))

    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = settings.max_request_bytes
    app.json.sort_keys = False
    app.json.ensure_ascii = False
    if settings.trust_proxy:  # só atrás de proxy confiável (Render): honra X-Forwarded-For/Proto/Host
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.extensions["omnilink"] = SimpleNamespace(
        settings=settings, coordinator=coordinator,
        msg_limiter=RateLimiter(settings.rate_limit_per_minute),
        ctl_limiter=RateLimiter(settings.control_rate_limit_per_minute),
        reg_limiter=RateLimiter(settings.ip_rate_limit_per_minute),
        authfail_limiter=RateLimiter(settings.ip_rate_limit_per_minute))

    @app.before_request
    def _guard():
        coordinator.maybe_purge()
        if settings.require_https and request.path != "/health" and not request.is_secure:
            raise OmniLinkError("TLS_REQUIRED", status=426)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path == "/receiver" or request.path.startswith("/static/"):
            resp.headers["Content-Security-Policy"] = PAGE_CSP
        else:
            resp.headers.setdefault("Cache-Control", "no-store")
        if settings.require_https:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @app.errorhandler(OmniLinkError)
    def _domain_error(err: OmniLinkError):
        body = {"ok": False, "error": err.diagnostic.to_dict()}
        if isinstance(err, Incompatible):
            body["status"] = "incompatible"
            body["negotiation"] = err.negotiation.to_dict()
        resp = jsonify(body)
        resp.status_code = err.status
        if err.retry_after:
            resp.headers["Retry-After"] = str(max(1, math.ceil(err.retry_after)))
        if err.status == 401:
            resp.headers["WWW-Authenticate"] = 'Bearer realm="omnilink"'
        return resp

    @app.errorhandler(HTTPException)
    def _http_error(err: HTTPException):
        if err.code == 413:
            diag = make("PAYLOAD_TOO_LARGE", limit=settings.max_request_bytes)
        elif err.code == 404:
            diag = make("NOT_FOUND")
        else:
            diag = make("INVALID_REQUEST", reason=f"{err.code} {err.name}")
        resp = jsonify({"ok": False, "error": diag.to_dict()})
        resp.status_code = err.code or 400
        return resp

    @app.errorhandler(Exception)
    def _unhandled(err: Exception):
        log.exception("erro não tratado em %s %s", request.method, request.path)
        resp = jsonify({"ok": False, "error": make("INTERNAL_ERROR").to_dict()})
        resp.status_code = 500
        return resp

    from .routes import bp
    app.register_blueprint(bp)
    return app
