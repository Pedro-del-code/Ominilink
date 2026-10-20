"""Catálogo de códigos de diagnóstico (mensagens em pt-BR) e fábrica de Diagnostic."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DiagCode(str, Enum):
    ROLE_MISMATCH = "ROLE_MISMATCH"
    PROTOCOL_VERSION_MISMATCH = "PROTOCOL_VERSION_MISMATCH"
    NO_TRANSPORT_ADVERTISED = "NO_TRANSPORT_ADVERTISED"
    NO_COMMON_TRANSPORT = "NO_COMMON_TRANSPORT"
    TRANSPORT_UNKNOWN = "TRANSPORT_UNKNOWN"
    TRANSPORT_NOT_IMPLEMENTED = "TRANSPORT_NOT_IMPLEMENTED"
    SENDER_LACKS_TRANSPORT = "SENDER_LACKS_TRANSPORT"
    TLS_REQUIRED = "TLS_REQUIRED"
    NO_COMMON_CODEC = "NO_COMMON_CODEC"
    CODEC_UNKNOWN = "CODEC_UNKNOWN"
    CODEC_NEEDS_BINARY_TRANSPORT = "CODEC_NEEDS_BINARY_TRANSPORT"
    CONTENT_UNSUPPORTED = "CONTENT_UNSUPPORTED"
    CONTENT_NEEDS_ADAPTATION = "CONTENT_NEEDS_ADAPTATION"
    ADAPTATION_APPLIED = "ADAPTATION_APPLIED"
    ADAPTATION_FAILED = "ADAPTATION_FAILED"
    SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"
    RECEIVER_UNKNOWN = "RECEIVER_UNKNOWN"
    RECEIVER_OFFLINE = "RECEIVER_OFFLINE"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    DEVICE_REVOKED = "DEVICE_REVOKED"
    ENROLLMENT_DENIED = "ENROLLMENT_DENIED"
    DEVICE_LIMIT = "DEVICE_LIMIT"
    RATE_LIMITED = "RATE_LIMITED"
    QUEUE_FULL = "QUEUE_FULL"
    INVALID_REQUEST = "INVALID_REQUEST"
    MALFORMED_MESSAGE = "MALFORMED_MESSAGE"
    INTEGRITY_FAILED = "INTEGRITY_FAILED"
    INVALID_IMAGE = "INVALID_IMAGE"
    CONTENT_SANITIZED = "CONTENT_SANITIZED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    NOT_FOUND = "NOT_FOUND"
    RECONNECT_LIMIT_EXCEEDED = "RECONNECT_LIMIT_EXCEEDED"
    DELIVERY_ATTEMPTS_EXCEEDED = "DELIVERY_ATTEMPTS_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass
class Diagnostic:
    code: str
    severity: str  # error | warning | info
    message: str
    suggestion: str | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# código -> (severidade, modelo de mensagem, sugestão padrão)
CATALOG: dict[str, tuple[str, str, str | None]] = {
    "ROLE_MISMATCH": ("error", "Papel incompatível: {who} não declara o papel '{role}'.",
                      "Anuncie o papel correto nas capacidades do dispositivo."),
    "PROTOCOL_VERSION_MISMATCH": ("error",
        "Sem versão de protocolo em comum (emissor={sender}, receptor={receiver}, coordenador={server}).",
        "Atualize o cliente mais antigo ou habilite uma versão comum."),
    "NO_TRANSPORT_ADVERTISED": ("error", "O receptor não anunciou nenhum transporte.",
        "Registre o dispositivo com ao menos um transporte (sse ou http-longpoll)."),
    "NO_COMMON_TRANSPORT": ("error",
        "Nenhum transporte utilizável em comum. Receptor anuncia {receiver}; coordenador implementa {server}.",
        "Abra o receptor web (/receiver) no navegador do dispositivo ou atualize o cliente para anunciar 'http-longpoll'."),
    "TRANSPORT_UNKNOWN": ("warning", "Transporte '{name}' desconhecido para este coordenador; ignorado.", None),
    "TRANSPORT_NOT_IMPLEMENTED": ("warning",
        "Transporte '{name}' é conhecido, mas não está implementado nesta versão; ignorado.",
        "Use 'sse' ou 'http-longpoll', ou implemente o transporte como plugin."),
    "SENDER_LACKS_TRANSPORT": ("warning", "O transporte '{name}' exige suporte do emissor, que não o anuncia.", None),
    "TLS_REQUIRED": ("error",
        "A política exige canal criptografado (HTTPS), mas o canal atual não é seguro.",
        "Acesse o coordenador por https:// (no Render o HTTPS é automático)."),
    "NO_COMMON_CODEC": ("error",
        "Nenhum codec em comum para os transportes disponíveis. Receptor anuncia {receiver}; coordenador implementa {server}.",
        "Anuncie 'json-b64' (universal) nas capacidades do receptor."),
    "CODEC_UNKNOWN": ("warning", "Codec '{name}' desconhecido; ignorado.", None),
    "CODEC_NEEDS_BINARY_TRANSPORT": ("info",
        "O codec '{name}' exige transporte com suporte binário; indisponível em '{transport}'.", None),
    "CONTENT_UNSUPPORTED": ("error", "O receptor não declara suporte ao conteúdo '{kind}'.",
        "Habilite o tipo no receptor ou envie outro tipo de conteúdo."),
    "CONTENT_NEEDS_ADAPTATION": ("error",
        "O conteúdo exige adaptação ({ops}), mas ela foi desativada pelo emissor.",
        "Reenvie com allow_adaptation=true."),
    "ADAPTATION_APPLIED": ("info", "Conteúdo adaptado ao receptor: {ops}.", None),
    "ADAPTATION_FAILED": ("error", "Não foi possível adaptar o conteúdo ao receptor: {reason}.", None),
    "SIZE_LIMIT_EXCEEDED": ("error", "Conteúdo excede o limite do receptor ({size} > {limit} {unit}).",
        "Reduza o conteúdo ou divida-o em partes."),
    "RECEIVER_UNKNOWN": ("error", "Dispositivo receptor '{device_id}' não encontrado.",
        "Liste os dispositivos disponíveis em GET /v1/devices."),
    "RECEIVER_OFFLINE": ("warning",
        "O receptor não está conectado agora; a mensagem ficará na fila por até {ttl} s.", None),
    "NOT_AUTHORIZED": ("error", "O receptor ainda não autorizou este emissor a enviar '{kind}'.",
        "O receptor foi notificado (pedido {request_id}). Aguarde a aprovação e reenvie."),
    "AUTH_REQUIRED": ("error", "Credencial ausente.", "Envie o cabeçalho 'Authorization: Bearer <token>'."),
    "AUTH_INVALID": ("error", "Credencial inválida.", "Registre o dispositivo novamente."),
    "AUTH_EXPIRED": ("error", "Credencial expirada.", "Gere uma nova credencial (registro ou rotação)."),
    "DEVICE_REVOKED": ("error", "Este dispositivo foi revogado.", "Registre um novo dispositivo."),
    "ENROLLMENT_DENIED": ("error", "Chave de registro inválida.", None),
    "DEVICE_LIMIT": ("error", "Limite de dispositivos registrados atingido.", "Revogue dispositivos que não usa mais."),
    "RATE_LIMITED": ("error", "Limite de requisições excedido; tente novamente em {retry_after:.0f} s.", None),
    "QUEUE_FULL": ("error", "A fila do receptor está cheia ({limit} mensagens pendentes).",
        "Aguarde o receptor consumir as mensagens pendentes."),
    "INVALID_REQUEST": ("error", "{reason}", None),
    "MALFORMED_MESSAGE": ("error", "Mensagem malformada: {reason}", None),
    "INTEGRITY_FAILED": ("error", "Falha de integridade: {reason}", None),
    "INVALID_IMAGE": ("error", "Imagem inválida ou não suportada: {reason}.", "Envie PNG, JPEG, GIF ou WebP íntegros."),
    "CONTENT_SANITIZED": ("info",
        "O HTML foi sanitizado: elementos e atributos fora da lista de permissão foram removidos.", None),
    "PAYLOAD_TOO_LARGE": ("error", "Requisição excede o tamanho máximo permitido ({limit} bytes).", None),
    "NOT_FOUND": ("error", "Recurso não encontrado.", None),
    "RECONNECT_LIMIT_EXCEEDED": ("error", "Limite de tentativas de reconexão atingido ({attempts}).",
        "Verifique a rede e o endereço do coordenador; reinicie o cliente."),
    "DELIVERY_ATTEMPTS_EXCEEDED": ("error", "Número máximo de tentativas de entrega excedido.", None),
    "INTERNAL_ERROR": ("error", "Erro interno. Consulte os logs do servidor.", None),
}


def _jsonable(detail: dict[str, Any]) -> dict:
    return json.loads(json.dumps(detail, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o)))


def make(code: str | DiagCode, message: str | None = None, suggestion: str | None = None,
         severity: str | None = None, **detail: Any) -> Diagnostic:
    code = code.value if isinstance(code, DiagCode) else str(code)
    sev, template, default_suggestion = CATALOG.get(code, ("error", code, None))
    if message is None:
        try:
            message = template.format(**detail)
        except (KeyError, IndexError, ValueError):
            message = template
    if suggestion is None and default_suggestion:
        try:
            suggestion = default_suggestion.format(**detail)
        except (KeyError, IndexError, ValueError):
            suggestion = default_suggestion
    return Diagnostic(code, severity or sev, message, suggestion, _jsonable(detail))
