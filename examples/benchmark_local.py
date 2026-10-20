#!/usr/bin/env python3
"""Medições reproduzíveis em loopback: latência de entrega por transporte e custo de bytes por codec.

    python examples/benchmark_local.py

ATENÇÃO: loopback não inclui latência de rede/TLS/proxy; os números mostram o custo do próprio software.
"""
from __future__ import annotations

import dataclasses
import io
import logging
import os
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app.api import create_app  # noqa: E402
from app.capabilities.profiles import python_receiver, python_sender  # noqa: E402
from app.client import OmniLinkClient, RetryPolicy  # noqa: E402
from app.codec import Message, get_codec, new_message_id  # noqa: E402
from app.config import Settings  # noqa: E402
from app.receiver import GrantPolicy, ReceiverRunner, ReceiverService  # noqa: E402

KEY = "chave-do-benchmark-123456"
logging.getLogger("werkzeug").setLevel(logging.ERROR)


class TimingPresenter:
    def __init__(self):
        self.arrivals = {}
        self.event = threading.Event()

    def present(self, msg):
        self.arrivals[msg.data.decode()] = time.perf_counter()
        self.event.set()
        return "displayed"


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def latency(url, transport, codec, n=40):
    receiver = OmniLinkClient(url, retry=RetryPolicy(max_attempts=5, base_delay=0.05))
    receiver.register(KEY, python_receiver(f"bench-{transport}"))
    presenter, stop = TimingPresenter(), threading.Event()
    service = ReceiverService(receiver.device_id, python_receiver(), presenter, GrantPolicy())
    runner = ReceiverRunner(receiver, service, transport=transport, codec=codec, stop=stop)
    if transport == "http-longpoll":
        runner.transport.wait_seconds = 5
    threading.Thread(target=runner.run, daemon=True).start()
    while runner.connections < 1:
        time.sleep(0.02)
    sender = OmniLinkClient(url)
    sender.register(KEY, python_sender())
    receiver.grant(sender.device_id, ["text"])
    samples = []
    for i in range(n):
        tag = f"{transport}-{i}"
        presenter.event.clear()
        t0 = time.perf_counter()
        sender.send_text(receiver.device_id, tag)
        deadline = time.monotonic() + 5
        while tag not in presenter.arrivals and time.monotonic() < deadline:
            presenter.event.wait(0.5)
        samples.append((presenter.arrivals[tag] - t0) * 1000)
        time.sleep(0.05)
    stop.set()
    return samples


def main() -> int:
    settings = dataclasses.replace(Settings.from_env({"OMNILINK_ENV": "test", "OMNILINK_ENROLLMENT_KEY": KEY,
                                                      "OMNILINK_LOG_LEVEL": "CRITICAL"}),
                                   sse_keepalive_seconds=5, rate_limit_per_minute=0, control_rate_limit_per_minute=0,
                                   ip_rate_limit_per_minute=0)
    server = make_server("127.0.0.1", 0, create_app(settings), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"

    print("Latência de entrega (POST /v1/messages → apresentação no receptor), texto curto, 40 envios, loopback")
    print(f"{'transporte':<16}{'mediana':>9}{'p95':>9}{'máx':>9}")
    for transport, codec in (("sse", "json-b64"), ("http-longpoll", "json-b64"), ("http-longpoll", "binary-frame")):
        s = latency(url, transport, codec)
        print(f"{transport + '/' + codec.split('-')[0]:<16}{statistics.median(s):>7.1f}ms{percentile(s, 0.95):>7.1f}ms{max(s):>7.1f}ms")
    server.shutdown()

    print("\nCusto em bytes no fio por codec (imagem PNG incompressível)")
    print(f"{'imagem':<14}{'conteúdo':>10}{'json-b64':>12}{'binary-frame':>14}{'economia':>10}")
    for w, h in ((200, 150), (640, 480), (900, 700)):
        buf = io.BytesIO()
        Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(buf, "PNG")
        msg = Message(new_message_id(), "dev_" + "a" * 16, "dev_" + "b" * 16, "image", "image/png", buf.getvalue())
        j, b = len(get_codec("json-b64").encode(msg).encode()), len(get_codec("binary-frame").encode(msg))
        print(f"{w}x{h:<9}{len(msg.data):>10,}{j:>12,}{b:>14,}{(1 - b / j) * 100:>9.1f}%".replace(",", "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
