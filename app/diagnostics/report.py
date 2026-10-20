"""Formatação legível de diagnósticos (usada pela CLI e pelos exemplos)."""
from __future__ import annotations

from typing import Iterable

_LABEL = {"error": "ERRO", "warning": "AVISO", "info": "INFO"}


def explain(diagnostics: Iterable[dict | object], alternatives: Iterable[str] = ()) -> str:
    lines: list[str] = []
    for d in diagnostics:
        get = d.get if isinstance(d, dict) else (lambda k, default=None, _d=d: getattr(_d, k, default))
        lines.append(f"[{_LABEL.get(get('severity'), get('severity'))}] {get('code')}: {get('message')}")
        if get("suggestion"):
            lines.append(f"        sugestão: {get('suggestion')}")
    alts = list(alternatives)
    if alts:
        lines.append("Alternativas propostas:")
        lines.extend(f"  - {a}" for a in alts)
    return "\n".join(lines)
