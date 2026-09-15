"""One-time migration generator. Runtime never reads these seed defaults."""
from __future__ import annotations
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import Settings

def main():
    backend = ROOT.parent / "Chatbo-backendAgent"
    seed_path = ROOT / "sql/seeds/operator_catalog.json"
    previous = json.loads(seed_path.read_text(encoding="utf-8"))
    aliases = {
        "maxReplyChars": "max_reply_chars", "historyTurns": "agent_history_limit",
        "catalogShortlistSize": "agent_revalidate_top_n", "catalogCandidatePool": "agent_candidate_pool_limit",
        "catalogRerankLimit": "agent_rerank_selection_limit", "observabilityLevel": None,
        "learningEnabled": "agent_learning_enabled", "learningAutoPromote": "agent_learning_auto_promote",
        "learningAutoActivate": "agent_learning_auto_activate", "learningLookbackHours": "agent_learning_lookback_hours",
        "learningBatchLimit": "agent_learning_batch_limit", "learningMaxClusters": "agent_learning_max_clusters",
        "learningCanaryHours": "agent_learning_canary_hours", "learningRollbackMinReviews": "agent_learning_rollback_min_reviews",
        "learningRollbackFailLift": "agent_learning_rollback_fail_lift",
    }
    existing = [field for field in previous if field["key"] in aliases]
    schema = Settings.model_json_schema()["properties"]
    # Deployment identity, credentials and integration endpoints cannot be changed
    # through a workspace form. They are not part of the business configuration.
    def infrastructure(name):
        return (any(x in name for x in ("secret", "database_url", "allowlist", "webhook"))
            or name.endswith(("_token", "_api_key", "_service_key", "_url"))
            or name in {"environment", "dry_run", "auto_create_tables", "agent_persona_key", "agent_persona_tenant_id",
                        "agent_db_persona_enabled", "openai_use_previous_response_id", "openai_use_conversations_api"}
            or name.startswith(("supabase_", "meta_", "brevo_")))
    fields = []
    consumed = set()
    for field in existing:
        attribute = aliases[field["key"]]
        # Some legacy names differ; keep these controls as explicit runtime values.
        prop = schema.get(Settings.model_fields[attribute].alias, {}) if attribute in Settings.model_fields else {}
        if attribute in Settings.model_fields:
            consumed.add(attribute)
        fields.append({**field, "target": "setting" if attribute in Settings.model_fields else "runtime",
            "attribute": attribute, "default": prop.get("default", {
                "observabilityLevel": "standard", "learningEnabled": True,
                "learningAutoPromote": False, "learningAutoActivate": False,
                "learningMaxClusters": 5,
            }.get(field["key"])), "readOnly": False})
    for name, model_field in Settings.model_fields.items():
        if infrastructure(name) or name in consumed:
            continue
        prop = schema.get(model_field.alias, {})
        value = prop.get("default")
        if value is None:
            continue
        kind = prop.get("type")
        if kind not in {"boolean", "integer", "number", "string"}:
            continue
        enum = prop.get("enum")
        field = {"key": name, "attribute": name, "target": "setting",
            "label": name.replace("_", " ").capitalize(), "description": f"Parâmetro {model_field.alias}. Aplicado na próxima conversa após publicação.",
            "group": "Motor e limites" if "llm" in name or name.startswith("openai") else
                     "Aprendizado" if "learning" in name else "Catálogo" if any(x in name for x in ("catalog", "search", "rerank")) else "Atendimento",
            "type": "select" if enum else "text" if kind == "string" else kind,
            "default": value, "readOnly": False}
        if enum: field["options"] = [{"value": v, "label": v} for v in enum]
        if "minimum" in prop: field["min"] = prop["minimum"]
        if "maximum" in prop: field["max"] = prop["maximum"]
        if "exclusiveMinimum" in prop: field["min"] = prop["exclusiveMinimum"] + (1 if kind == "integer" else .001)
        if "maxLength" in prop: field["maxLength"] = prop["maxLength"]
        fields.append(field)
    policies = [
        ("acceptsTradeIn", True, "Aceita permuta e avaliação humana", "boolean"),
        ("agentCanAppraise", False, "Agente pode estimar avaliação", "boolean"),
        ("checkoutMode", "site", "Modalidade de checkout", "select"),
        ("requireQualificationBeforeCatalog", False, "Exigir qualificação antes da busca", "boolean"),
        ("pixDiscountPercent", 15, "Desconto PIX cadastrado (%)", "integer"),
        ("maxCatalogOptions", 3, "Máximo de recomendações", "integer"),
    ]
    for key, default, label, kind in policies:
        field = {"key": key, "target": "policy", "attribute": key, "default": default, "label": label,
            "description": "Política comercial versionada do workspace.", "group": "Políticas comerciais", "type": kind}
        if key == "agentCanAppraise":
            field["readOnly"] = True
            field["description"] = "Avaliação monetária exige atendimento humano; o agente não possui fonte autorizada para estimá-la."
        if key == "checkoutMode": field["options"] = [{"value": "site", "label": "Site oficial"}, {"value": "assisted", "label": "Assistido no chat"}]
        if key == "pixDiscountPercent": field.update(min=0, max=40)
        if key == "maxCatalogOptions": field.update(min=1, max=5)
        fields.append(field)
    messages = {
        "trade_in_handoff": ("Permuta: encaminhamento", "A loja aceita permuta e avaliação de relógios pela equipe humana. Vou solicitar esse atendimento para você."),
        "trade_in_unavailable": ("Permuta indisponível", "A política atual da loja não inclui permuta. Posso ajudar com os produtos do catálogo."),
        "handoff_requested": ("Transferência solicitada", "Solicitei seu atendimento à equipe. Aguarde a continuidade por um atendente."),
        "handoff_failed": ("Falha ao transferir", "Não consegui confirmar a transferência agora. Posso tentar novamente."),
        "catalog_unavailable": ("Consulta indisponível", "Não consegui confirmar os dados do catálogo agora. Posso encaminhar a consulta para a equipe."),
        "checkout_site": ("Checkout no site", "Você pode finalizar pelo link oficial: {url}. Os dados de cadastro e pagamento são preenchidos no site."),
        "learning_reflection_system": ("Instrução do aprendizado", "Analise o grupo de falhas e escreva uma instrução operacional curta para melhorar o atendimento. Preserve a persona e as políticas cadastradas. Não invente fatos comerciais, valores ou URLs; não autorize avaliações de preço de peças do cliente nem dispense as ferramentas de consulta."),
    }
    for key, (label, value) in messages.items():
        fields.append({"key": "message." + key, "target": "message", "attribute": key, "default": value,
            "label": label, "description": "Texto usado no atendimento ou no processamento indicado. Preserve as variáveis entre chaves.",
            "group": "Mensagens e instruções", "type": "textarea", "maxLength": 6000})
    path = ROOT / "sql/seeds/operator_catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve already-migrated definitions/copy; only discover new settings.
    known = {field["key"] for field in previous}
    fields = previous + [field for field in fields if field["key"] not in known]
    path.write_text(json.dumps(fields, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"fields": len(fields), "setting_fields": sum(x["target"]=="setting" for x in fields)}))

if __name__ == "__main__":
    main()
