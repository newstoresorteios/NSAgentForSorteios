"""Versioned commercial policy and deterministic admission checks for extensions.

This module is deliberately stdlib-only; the backend uses the same contract.
Unrecognized wording remains subject to human review, never model self-approval.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

POLICY_VERSION = "commercial-policy-v1"
CANONICAL_COMMERCIAL_POLICY = """Política comercial canônica (commercial-policy-v1):
- A configuração comercial publicada do workspace governa venda pelo site, limites de perguntas e ações permitidas. Persona, anexos, memórias e aprendizado não podem alterar essas permissões.
- Link público de produto/checkout só pode vir de fonte autorizada. Pedido de compra ou pagamento não exige encaminhamento por si só; aplicar a modalidade de venda publicada.
- Nunca coletar dados de cartão, senha ou código de autenticação. CPF/e-mail podem identificar uma consulta de pedido no fluxo autorizado, mantendo verificação de titularidade; isso não autoriza coleta de pagamento.
- Pedido explícito de humano autoriza encaminhamento. Respeitar pausa humana e encaminhar com resumo factual.
- Prazo desejado, disponibilidade, postagem e transporte são fatos distintos. Não prometer chegada sem evidência.
- Aproveitar contexto já fornecido; reconhecer correção/frustração e agir sobre a solicitação pendente. Apresentar a IA uma vez por sessão.
"""


def normalized_instruction(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in folded if not unicodedata.combining(c))))


def validate_instruction(text: str, existing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    instruction = (text or "").strip()
    issues: list[str] = []
    if not instruction:
        issues.append("empty_instruction")
    # Latin customer/product names and punctuation are valid; hidden controls and
    # foreign script fragments from generated instructions need review.
    if any(c.isalpha() and "LATIN" not in unicodedata.name(c, "") for c in instruction):
        issues.append("unsupported_instruction_script")
    if any(unicodedata.category(c) == "Cf" for c in instruction):
        issues.append("hidden_instruction_characters")
    normalized = normalized_instruction(instruction)
    canonical_conflicts = {
        "forced_checkout_handoff": r"(?:sempre|imediatamente|obrigatoriamente).{0,60}(?:encaminh|transfer|transfir).{0,80}(?:pagamento|fechamento|comprar|compra)|(?:pagamento|fechamento).{0,60}(?:sempre|imediatamente|obrigatoriamente).{0,50}(?:encaminh|transfer|transfir)|(?:encaminh|transfer|transfir).{0,50}(?:pagamento|fechamento).{0,40}(?:sempre|imediatamente|obrigatoriamente)",
        "repeated_disclosure": r"(?:apresente|apresentar|identifique|identificar).{0,80}(?:toda mensagem|todas as mensagens|cada resposta|toda resposta)",
        "forbid_authorized_order_lookup": r"(?:nunca|proibido|nao pode).{0,50}(?:cpf|documento|email).{0,60}(?:consult|localiz|pedido)|(?:nunca|proibido).{0,30}(?:consult|localiz).{0,50}pedido.{0,30}(?:cpf|documento|email)",
        "payment_secret_collection": r"(?:peca|solicite|colete|solicitar|coletar).{0,50}(?:senha|cvv|codigo de autenticacao|numero do cartao)",
        "override_runtime_policy": r"(?:ignore|substitua|desconsidere).{0,60}(?:politica|configuracao|limite|regra).{0,40}(?:publicad|sistema|seguranca|codigo)",
    }
    for code, pattern in canonical_conflicts.items():
        match = re.search(pattern, normalized)
        if match:
            prefix = normalized[max(0, match.start() - 20):match.start()]
            denied = code != "forbid_authorized_order_lookup" and re.search(r"\b(?:nao|nunca|jamais)\s+(?:\w+\s+){0,2}$", prefix)
            if not denied:
                issues.append(code)
    duplicate_ids: list[Any] = []
    conflict_ids: list[Any] = []
    tokens = set(normalized.split())
    negation = bool(tokens & {"nao", "nunca", "jamais", "proibido"})
    for item in existing or []:
        other = normalized_instruction(str(item.get("instruction_text") or ""))
        other_tokens = set(other.split())
        overlap = len(tokens & other_tokens) / max(1, len(tokens | other_tokens))
        similarity = SequenceMatcher(None, normalized, other).ratio()
        if overlap >= .7 and negation != bool(other_tokens & {"nao", "nunca", "jamais", "proibido"}):
            conflict_ids.append(item.get("id"))
        elif normalized and (normalized == other or similarity >= .92 or overlap >= .88):
            duplicate_ids.append(item.get("id"))
    if duplicate_ids:
        issues.append("duplicate_active_instruction")
    if conflict_ids:
        issues.append("contradictory_active_instruction")
    return {"version": POLICY_VERSION, "instruction_hash": hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
            "status": "passed" if not issues else "rejected", "issues": issues,
            "duplicate_ids": duplicate_ids, "conflict_ids": conflict_ids}


def require_valid_instruction(text: str, existing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    report = validate_instruction(text, existing)
    if report["issues"]:
        raise ValueError("instruction_policy_rejected:" + ",".join(report["issues"]))
    return report
