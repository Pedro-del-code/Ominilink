"""Motor de negociação: dado emissor, receptor e conteúdo, escolhe versão, transporte, codec e adaptações.

Função pura (sem E/S): todas as decisões e rejeições ficam registradas em `considered` e `diagnostics`,
o que torna a negociação explicável e testável.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..capabilities.models import KIND_MIMES, ContentDescriptor, DeviceCapabilities
from ..diagnostics.codes import Diagnostic, make
from .registry import ProtocolRegistry, default_registry

SUPPORTED_VERSIONS = (1,)


@dataclass(frozen=True)
class NegotiationContext:
    active_transports: frozenset = frozenset()   # transportes com sessão ativa do receptor agora
    channel_secure: bool = True                  # a requisição chegou por canal criptografado?
    require_secure: bool = False                 # política exige criptografia?
    allow_adaptation: bool = True
    large_payload_bytes: int = 256 * 1024
    supported_versions: tuple = SUPPORTED_VERSIONS


@dataclass(frozen=True)
class Adaptation:
    op: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"op": self.op, **self.params}


@dataclass
class Selection:
    protocol_version: int
    transport: str
    codec: str
    adaptations: list
    score: float

    def to_dict(self) -> dict:
        return {"protocol_version": self.protocol_version, "transport": self.transport, "codec": self.codec,
                "adaptations": [a.to_dict() for a in self.adaptations], "score": self.score}


@dataclass
class NegotiationResult:
    compatible: bool
    selection: Selection | None
    diagnostics: list
    alternatives: list
    considered: list

    def to_dict(self) -> dict:
        return {"compatible": self.compatible,
                "selection": self.selection.to_dict() if self.selection else None,
                "diagnostics": [d.to_dict() for d in self.diagnostics],
                "alternatives": list(self.alternatives), "considered": list(self.considered)}


def plan_content(receiver: DeviceCapabilities, content: ContentDescriptor, allow: bool):
    """Verifica se o receptor aceita o conteúdo e, se preciso, planeja adaptações."""
    diags: list[Diagnostic] = []
    alts: list[str] = []
    adaptations: list[Adaptation] = []
    kind = content.kind
    cap = receiver.content.get(kind)

    if kind == "html" and cap is None and receiver.content.get("text") is not None:
        if not allow:
            diags.append(make("CONTENT_NEEDS_ADAPTATION", ops="html_to_text"))
            return adaptations, diags, alts
        adaptations.append(Adaptation("html_to_text"))
        kind, cap = "text", receiver.content["text"]
    if cap is None:
        diags.append(make("CONTENT_UNSUPPORTED", kind=kind))
        alts.append({"image": "Use um receptor com suporte a imagens (por exemplo, o receptor web em /receiver).",
                     "html": "Envie o conteúdo como texto simples.",
                     "text": "Escolha um receptor que declare suporte a texto."}[kind])
        return adaptations, diags, alts

    if kind == "image":
        reasons = []
        if content.mime not in (cap.mimes or KIND_MIMES["image"]):
            reasons.append("transcode")
        if (cap.max_width and content.width and content.width > cap.max_width) or \
           (cap.max_height and content.height and content.height > cap.max_height):
            reasons.append("downscale")
        if content.size > cap.max_bytes:
            reasons.append("shrink")
        if reasons:
            if not allow:
                diags.append(make("CONTENT_NEEDS_ADAPTATION", ops="+".join(reasons)))
            else:
                adaptations.append(Adaptation("fit_image", {
                    "mimes": list(cap.mimes), "max_width": cap.max_width, "max_height": cap.max_height,
                    "max_bytes": cap.max_bytes, "reasons": reasons}))
    elif kind == "text" and cap.max_chars and content.chars and content.chars > cap.max_chars:
        diags.append(make("SIZE_LIMIT_EXCEEDED", size=content.chars, limit=cap.max_chars, unit="caracteres"))
        alts.append("Divida o texto em partes menores.")
    elif content.size > cap.max_bytes and not adaptations:
        diags.append(make("SIZE_LIMIT_EXCEEDED", size=content.size, limit=cap.max_bytes, unit="bytes"))
        alts.append("Reduza o conteúdo ou divida-o em partes.")
    return adaptations, diags, alts


def negotiate(sender: DeviceCapabilities, receiver: DeviceCapabilities, content: ContentDescriptor,
              ctx: NegotiationContext | None = None, registry: ProtocolRegistry | None = None) -> NegotiationResult:
    ctx = ctx or NegotiationContext()
    reg = registry or default_registry()
    diags: list[Diagnostic] = []
    alts: list[str] = []
    considered: list[dict] = []

    # 1. papéis
    if "sender" not in sender.roles:
        diags.append(make("ROLE_MISMATCH", who="emissor", role="sender"))
    if "receiver" not in receiver.roles:
        diags.append(make("ROLE_MISMATCH", who="receptor", role="receiver"))

    # 2. versão do protocolo
    common = sorted(set(sender.protocol_versions) & set(receiver.protocol_versions) & set(ctx.supported_versions))
    if not common:
        diags.append(make("PROTOCOL_VERSION_MISMATCH", sender=list(sender.protocol_versions),
                          receiver=list(receiver.protocol_versions), server=list(ctx.supported_versions)))
        alts.append("Atualize o cliente que usa a versão mais antiga do protocolo ou escolha outro receptor.")

    # 3. conteúdo
    adaptations, content_diags, content_alts = plan_content(receiver, content, ctx.allow_adaptation)
    diags += content_diags
    alts += content_alts

    # 4. transporte × codec
    usable: list = []
    candidates: list = []
    tls_blocked = False
    if not receiver.transports:
        diags.append(make("NO_TRANSPORT_ADVERTISED"))
    for t_name in receiver.transports:
        spec = reg.transport(t_name)
        if spec is None:
            diags.append(make("TRANSPORT_UNKNOWN", name=t_name))
            considered.append({"transport": t_name, "rejected": "TRANSPORT_UNKNOWN"})
        elif not spec.implemented:
            diags.append(make("TRANSPORT_NOT_IMPLEMENTED", name=t_name))
            considered.append({"transport": t_name, "rejected": "TRANSPORT_NOT_IMPLEMENTED"})
        elif spec.needs_sender_support and t_name not in sender.transports:
            diags.append(make("SENDER_LACKS_TRANSPORT", name=t_name))
            considered.append({"transport": t_name, "rejected": "SENDER_LACKS_TRANSPORT"})
        elif ctx.require_secure and not (ctx.channel_secure and spec.secure_capable):
            tls_blocked = True
            considered.append({"transport": t_name, "rejected": "TLS_REQUIRED"})
        else:
            usable.append(spec)
    for spec in usable:
        for c_name in receiver.codecs:
            cspec = reg.codec(c_name)
            if cspec is None:
                if not any(d.code == "CODEC_UNKNOWN" and d.detail.get("name") == c_name for d in diags):
                    diags.append(make("CODEC_UNKNOWN", name=c_name))
                considered.append({"transport": spec.name, "codec": c_name, "rejected": "CODEC_UNKNOWN"})
            elif cspec.binary and not spec.supports_binary:
                diags.append(make("CODEC_NEEDS_BINARY_TRANSPORT", name=c_name, transport=spec.name))
                considered.append({"transport": spec.name, "codec": c_name, "rejected": "CODEC_NEEDS_BINARY_TRANSPORT"})
            else:
                candidates.append((spec, cspec))

    if not candidates and receiver.transports:
        if tls_blocked and not usable:
            diags.append(make("TLS_REQUIRED"))
            alts.append("Acesse o coordenador por HTTPS.")
        elif not usable:
            diags.append(make("NO_COMMON_TRANSPORT", receiver=list(receiver.transports),
                              server=reg.implemented_transports()))
            alts.append("Abra o receptor web (/receiver) no navegador do dispositivo receptor.")
            alts.append("Registre o dispositivo com um perfil que anuncie 'http-longpoll' e 'json-b64'.")
        else:
            diags.append(make("NO_COMMON_CODEC", receiver=list(receiver.codecs), server=reg.implemented_codecs()))
            alts.append("Anuncie 'json-b64' (codec universal) nas capacidades do receptor.")

    # 5. pontuação: pequenos conteúdos priorizam latência; grandes priorizam eficiência de bytes
    selection = None
    if candidates:
        weight = min(1.0, content.size / ctx.large_payload_bytes) if ctx.large_payload_bytes else 0.0

        def score(pair) -> float:
            t, c = pair
            value = (1 - weight) * (0.7 * t.score + 0.3 * c.score) + weight * (0.3 * t.score + 0.7 * c.score)
            return round(value + (40 if t.name in ctx.active_transports else 0), 2)

        ranked = sorted(candidates, key=lambda p: (score(p), p[0].score), reverse=True)
        for t, c in ranked:
            considered.append({"transport": t.name, "codec": c.name, "score": score((t, c))})
        best_t, best_c = ranked[0]
        if common:
            selection = Selection(common[-1], best_t.name, best_c.name, adaptations, score((best_t, best_c)))

    compatible = selection is not None and not any(d.severity == "error" for d in diags)
    if not compatible:
        selection = None
    elif adaptations:
        diags.append(make("ADAPTATION_APPLIED", ops=", ".join(a.op for a in adaptations)))
    return NegotiationResult(compatible, selection, diags, alts, considered)
