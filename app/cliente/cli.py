"""CLI do OmniLink: registrar dispositivos, descobrir, negociar, enviar e receber."""
from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import os
import sys
import time
from pathlib import Path

from ..capabilities.models import KIND_MIMES, DeviceCapabilities
from ..capabilities.profiles import PROFILES
from ..content import inspect_image
from ..diagnostics import configure_logging, explain
from ..discovery import DEFAULT_PORT, UdpResponder, classify, discover
from ..receiver import ConsolePresenter, GrantPolicy, ReceiverRunner, ReceiverService
from ..transport import available_transports
from .api import ApiError, OmniLinkClient
from .retry import ReconnectLimitExceeded, RetryPolicy

DEFAULT_SERVER = "http://127.0.0.1:8000"


def _creds_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("OMNILINK_CREDENTIALS") or Path.home() / ".omnilink" / "default.json").expanduser()


def save_credentials(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # somente o dono lê (contém o token)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_client(args) -> tuple:
    path = _creds_path(args.credentials)
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Credenciais não encontradas em {path}. Execute 'register' primeiro.") from None
    retry = RetryPolicy(max_attempts=args.retries, max_delay=args.max_delay)
    return OmniLinkClient(creds["server"], creds["token"], retry=retry), creds


def _print_negotiation(neg: dict) -> None:
    sel = neg.get("selection")
    if sel:
        ops = ", ".join(a["op"] for a in sel["adaptations"]) or "nenhuma"
        print(f"Compatível: transporte={sel['transport']} codec={sel['codec']} versão={sel['protocol_version']} "
              f"adaptações={ops}")
    else:
        print("INCOMPATÍVEL.")
    text = explain(neg.get("diagnostics", []), neg.get("alternatives", []))
    if text:
        print(text)


def _describe_file(kind: str, path: Path) -> dict:
    data = path.read_bytes()
    if kind == "image":
        info = inspect_image(data, max_bytes=50_000_000, max_pixels=100_000_000)
        return {"mime": info.mime, "size": info.size, "width": info.width, "height": info.height}
    return {"mime": KIND_MIMES[kind][0], "size": len(data)}


def cmd_register(args) -> int:
    key = args.enrollment_key or os.environ.get("OMNILINK_ENROLLMENT_KEY") or getpass.getpass("Chave de registro: ")
    caps = PROFILES[args.profile](args.name)
    client = OmniLinkClient(args.server)
    data = client.register(key, caps)
    path = _creds_path(args.out)
    save_credentials(path, {"server": args.server, "device_id": data["device_id"], "token": data["token"],
                            "name": args.name, "profile": args.profile})
    print(f"Dispositivo registrado: {data['device_id']} ({args.name}, perfil {args.profile})")
    print(f"Credenciais salvas em {path} (somente leitura do dono). O token não é exibido.")
    return 0


def cmd_devices(args) -> int:
    client, creds = load_client(args)
    print(discover(client).explain())
    return 0


def cmd_caps(args) -> int:
    client, _ = load_client(args)
    print(json.dumps(client.capabilities(args.device_id), indent=2, ensure_ascii=False))
    return 0


def cmd_negotiate(args) -> int:
    client, _ = load_client(args)
    desc = _describe_file(args.kind, Path(args.file)) if args.file else {"size": args.size}
    _print_negotiation(client.negotiate(args.to, args.kind, allow_adaptation=not args.no_adapt, **desc))
    return 0


def cmd_send(args) -> int:
    client, _ = load_client(args)
    opts = {"allow_adaptation": not args.no_adapt}
    try:
        if args.what == "text":
            resp = client.send_text(args.to, args.value, **opts)
        elif args.what == "image":
            resp = client.send_image(args.to, args.value, **opts)
        else:
            html = sys.stdin.read() if args.value == "-" else Path(args.value).read_text(encoding="utf-8")
            resp = client.send_html(args.to, html, **opts)
    except ApiError as exc:
        if exc.code == "NOT_AUTHORIZED":
            print(f"Aguardando autorização: {exc.diagnostic.get('message')}")
            print("O receptor precisa aprovar este emissor (ex.: 'receive --allow <seu-id>' ou 'grant approve').")
            print(f"Seu id de dispositivo: {client.device_id}")
            return 3
        if exc.negotiation:
            _print_negotiation(exc.negotiation)
            return 2
        print(f"Erro: {exc}")
        return 1
    sel = resp["negotiation"]["selection"] if "negotiation" in resp else None
    print(f"Aceita: {resp['message_id']} (status {resp['status']})" +
          (f" via {sel['transport']}/{sel['codec']}" if sel else ""))
    text = explain([d for d in resp.get("diagnostics", []) if d["code"] != "CODEC_NEEDS_BINARY_TRANSPORT"])
    if text:
        print(text)
    if args.wait:
        final = client.message_status(resp["message_id"], wait=args.wait)
        print(f"Status final: {final['status']}" + (f" ({final['reason']})" if final.get("reason") else ""))
        return 0 if final["status"] in ("delivered", "displayed") else 4
    return 0


def _ask(request: dict) -> str | None:
    answer = input(f"Permitir que '{request.get('from_name')}' ({request['from']}) envie {', '.join(request['kinds'])}? [s/N] ")
    return "approve" if answer.strip().lower() in ("s", "sim", "y", "yes") else "deny"


def cmd_receive(args) -> int:
    configure_logging("INFO")
    client, creds = load_client(args)
    caps = DeviceCapabilities.from_dict(client.capabilities(creds["device_id"]))
    transport = args.transport or next((t for t in caps.transports if t in available_transports()), "sse")
    codec = args.codec or ("binary-frame" if transport == "http-longpoll" and "binary-frame" in caps.codecs else "json-b64")
    if args.auto_accept_all:
        print("ATENÇÃO: aceitando automaticamente QUALQUER dispositivo autenticado (apenas para demonstração).")
    policy = GrantPolicy(args.auto_accept_all, frozenset(args.allow or []), _ask if sys.stdin.isatty() else None)
    service = ReceiverService(creds["device_id"], caps, ConsolePresenter(args.inbox, args.open_html), policy)
    runner = ReceiverRunner(client, service, transport=transport, codec=codec, retry=client.retry)
    print(f"Receptor {creds['device_id']} aguardando via {transport}/{codec}. Ctrl+C para sair.")
    try:
        runner.run()
    except KeyboardInterrupt:
        print("\nEncerrado.")
    except ReconnectLimitExceeded as exc:
        print(explain([exc.diagnostic]))
        return 4
    except ApiError as exc:
        print(f"Erro: {exc}")
        return 1
    return 0


def cmd_grant(args) -> int:
    client, _ = load_client(args)
    if args.action == "list":
        data = client.grants()
        print("Autorizações:", json.dumps(data["grants"], ensure_ascii=False) if data["grants"] else "nenhuma")
        print("Pedidos pendentes:", json.dumps(data["requests"], ensure_ascii=False) if data["requests"] else "nenhum")
    elif args.action == "allow":
        print(client.grant(args.target, args.kinds))
    elif args.action == "revoke":
        print("Revogada." if client.revoke_grant(args.target) else "Não havia autorização.")
    else:
        print(client.decide_grant(args.target, "approve" if args.action == "approve" else "deny"))
    return 0


def cmd_discover(args) -> int:
    client = load_client(args)[0] if args.credentials or _creds_path(None).exists() else None
    print(discover(client, lan=args.lan, lan_port=args.lan_port, lan_timeout=args.lan_timeout).explain())
    return 0


def cmd_explain_service(args) -> int:
    info = classify(args.service_type)
    print(f"{args.service_type}: {info.family} [{info.status}]\n  {info.note}")
    return 0


def cmd_announce(args) -> int:
    responder = UdpResponder(args.name, args.url, args.role, port=args.port).start()
    print(f"Anunciando '{args.name}' ({args.url}) na LAN, UDP {responder.port}. Ctrl+C para sair.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        responder.stop()
    return 0


def cmd_self_revoke(args) -> int:
    client, _ = load_client(args)
    print(client.revoke_self())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omnilink", description="Cliente de linha de comando do OmniLink.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, creds=True):
        if creds:
            p.add_argument("-c", "--credentials", help="arquivo de credenciais (padrão: ~/.omnilink/default.json)")
        p.add_argument("--retries", type=int, default=8, help="tentativas consecutivas de reconexão (padrão 8)")
        p.add_argument("--max-delay", type=float, default=15.0, help="espera máxima entre tentativas, em s")

    p = sub.add_parser("register", help="registra este dispositivo e salva as credenciais")
    p.add_argument("--server", default=os.environ.get("OMNILINK_SERVER_URL", DEFAULT_SERVER))
    p.add_argument("--name", required=True)
    p.add_argument("--profile", choices=sorted(PROFILES), default="python-receiver")
    p.add_argument("--enrollment-key", help="(ou variável OMNILINK_ENROLLMENT_KEY; evite passar na linha de comando)")
    p.add_argument("--out", help="onde salvar as credenciais")
    p.set_defaults(fn=cmd_register)

    for name, fn, helptext in (("devices", cmd_devices, "lista dispositivos do coordenador"),
                               ("self-revoke", cmd_self_revoke, "revoga este dispositivo")):
        p = sub.add_parser(name, help=helptext)
        common(p)
        p.set_defaults(fn=fn)

    p = sub.add_parser("caps", help="mostra as capacidades anunciadas por um dispositivo")
    common(p)
    p.add_argument("device_id")
    p.set_defaults(fn=cmd_caps)

    p = sub.add_parser("negotiate", help="simula a negociação com um receptor (sem enviar)")
    common(p)
    p.add_argument("--to", required=True)
    p.add_argument("--kind", choices=["text", "image", "html"], required=True)
    p.add_argument("--file", help="arquivo que seria enviado")
    p.add_argument("--size", type=int, default=0, help="tamanho em bytes, se não houver arquivo")
    p.add_argument("--no-adapt", action="store_true")
    p.set_defaults(fn=cmd_negotiate)

    p = sub.add_parser("send", help="envia texto, imagem ou HTML")
    common(p)
    p.add_argument("--to", required=True)
    p.add_argument("what", choices=["text", "image", "html"])
    p.add_argument("value", help="texto, caminho do arquivo ('-' lê o HTML da entrada padrão)")
    p.add_argument("--wait", type=float, default=0, help="aguarda a confirmação por até N segundos")
    p.add_argument("--no-adapt", action="store_true")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("receive", help="recebe e apresenta conteúdo")
    common(p)
    p.add_argument("--transport", choices=available_transports())
    p.add_argument("--codec", choices=["json-b64", "binary-frame"])
    p.add_argument("--inbox", default="inbox", help="pasta para imagens/HTML recebidos")
    p.add_argument("--open-html", action="store_true", help="abre o HTML recebido no navegador padrão")
    p.add_argument("--allow", nargs="*", metavar="DEVICE_ID", help="emissores aprovados automaticamente")
    p.add_argument("--auto-accept-all", action="store_true", help="aprova qualquer emissor (só demonstração)")
    p.set_defaults(fn=cmd_receive)

    p = sub.add_parser("grant", help="gerencia autorizações de emissores")
    common(p)
    p.add_argument("action", choices=["list", "allow", "revoke", "approve", "deny"])
    p.add_argument("target", nargs="?", help="id do emissor (allow/revoke) ou do pedido (approve/deny)")
    p.add_argument("--kinds", nargs="+", default=["text"], choices=["text", "image", "html"])
    p.set_defaults(fn=cmd_grant)

    p = sub.add_parser("discover", help="descobre dispositivos (coordenador) e serviços na LAN")
    common(p)
    p.add_argument("--lan", action="store_true", help="sonda a LAN por UDP")
    p.add_argument("--lan-port", type=int, default=DEFAULT_PORT)
    p.add_argument("--lan-timeout", type=float, default=1.5)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("explain-service", help="explica o que um tipo de serviço mDNS/SSDP exigiria")
    p.add_argument("service_type")
    p.set_defaults(fn=cmd_explain_service)

    p = sub.add_parser("announce", help="anuncia este host na LAN (UDP) para descoberta")
    p.add_argument("--name", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--role", choices=["coordinator", "receiver"], default="coordinator")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(fn=cmd_announce)
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "grant" and args.action != "list" and not args.target:
        print("Informe o id do emissor/pedido.")
        return 1
    try:
        return args.fn(args)
    except ApiError as exc:
        print(f"Erro: {exc}")
        return 1
    except (ConnectionError, OSError) as exc:
        print(f"Falha de conexão: {exc}")
        return 4
