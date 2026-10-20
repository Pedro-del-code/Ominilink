"""Servidor de DESENVOLVIMENTO: `python -m app.api [--host 0.0.0.0] [--port 8000]`.

Em produção use gunicorn (veja render.yaml). O servidor embutido do Werkzeug não é endurecido para a internet.
"""
from __future__ import annotations

import argparse
import os

from ..config import Settings
from .factory import create_app


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.api", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 para aceitar outros aparelhos da rede local (HTTP puro!)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    app = create_app(settings)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    print(f"OmniLink (desenvolvimento) em http://{shown}:{args.port}  |  receptor web: http://{shown}:{args.port}/receiver  |  "
          f"saúde: http://{shown}:{args.port}/health", flush=True)
    if args.host == "0.0.0.0":
        print("AVISO: escutando em todas as interfaces com HTTP puro; use apenas em redes que você controla.", flush=True)
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
