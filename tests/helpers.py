"""Utilitários compartilhados pelos testes (sem dependências além das do projeto)."""
from __future__ import annotations

import dataclasses
import io
import logging
import random
import threading

from PIL import Image
from werkzeug.serving import make_server

from app.api import create_app
from app.config import Settings

logging.getLogger("werkzeug").setLevel(logging.ERROR)   # sem log de acesso do servidor de desenvolvimento nos testes
ENROLL_KEY = "test-enrollment-key-123456"
DEV_A = "dev_" + "a" * 16
DEV_B = "dev_" + "b" * 16


def make_settings(**overrides) -> Settings:
    base = Settings.from_env({"OMNILINK_ENV": "test", "OMNILINK_ENROLLMENT_KEY": ENROLL_KEY,
                              "OMNILINK_LOG_LEVEL": "CRITICAL"})
    return dataclasses.replace(base, **overrides)


def image_bytes(fmt: str = "PNG", size=(64, 48), color=(200, 30, 30), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


def noise_png(size=(220, 220), seed=7) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", size)
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class ServerThread:
    """Servidor Flask real (Werkzeug, multithread) em porta efêmera de loopback."""

    def __init__(self, settings: Settings | None = None, store=None, port: int = 0):
        self.settings = settings or make_settings(sse_keepalive_seconds=1)
        self.app = create_app(self.settings, store)
        self.coordinator = self.app.extensions["omnilink"].coordinator
        self._srv = make_server("127.0.0.1", port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_port}"

    @property
    def port(self) -> int:
        return self._srv.server_port

    def __enter__(self) -> "ServerThread":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()
