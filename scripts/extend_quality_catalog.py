"""Migration data only: runtime must read the published database catalog."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / "sql/seeds/operator_catalog.json"
    definitions = json.loads(path.read_text(encoding="utf-8"))
    # Fresh workspaces need interpretation + composition + review. Published
    # operator overrides remain untouched by the catalog migration.
    next(f for f in definitions if f["key"] == "agent_max_llm_calls_per_turn")["default"] = 3
    features = [
        {"field": "mechanism", "value": "automatic", "label": "automático", "aliases": ["automático", "automatic", "automatico", "automáticos", "automatics", "powermatic"], "query": "automático", "evidenceFields": ["mechanism", "movement", "movimento"]},
        {"field": "mechanism", "value": "quartz", "label": "quartzo", "aliases": ["quartzo", "quartz"], "query": "quartzo", "evidenceFields": ["mechanism", "movement", "movimento"]},
        {"field": "mechanism", "value": "solar", "label": "solar", "aliases": ["solar", "eco-drive", "eco drive"], "query": "solar", "evidenceFields": ["mechanism", "movement", "movimento"]},
        {"field": "mechanism", "value": "manual", "label": "corda manual", "aliases": ["corda manual", "manual winding", "hand winding"], "query": "manual", "evidenceFields": ["mechanism", "movement", "movimento"]},
        {"field": "crystal", "value": "sapphire", "label": "cristal de safira", "aliases": ["safira", "sapphire"], "query": "safira", "evidenceFields": ["crystal", "glass", "cristal", "vidro"]},
        {"field": "crystal", "value": "mineral", "label": "cristal mineral", "aliases": ["mineral", "hardlex"], "query": "mineral", "evidenceFields": ["crystal", "glass", "cristal", "vidro"]},
    ]
    fields = []
    def add(key, label, default, kind, **extra):
        fields.append(dict(key=key, attribute=key, target="policy", label=label, default=default, type=kind,
                           group="Qualidade e critérios técnicos", description="Política publicada no banco, aplicada por atendimento.", **extra))
    add("catalogTechnicalFeatures", "Características técnicas e sinônimos", json.dumps(features, ensure_ascii=False), "textarea", maxLength=30000, valueSchema="technicalFeatures")
    add("catalogFeatureRelaxationPhrases", "Expressões que dispensam uma característica", json.dumps(["não precisa ser {feature}", "não precisa de {feature}", "não precisa ter {feature}", "pode ser sem {feature}", "sem exigência de {feature}"], ensure_ascii=False), "textarea", maxLength=6000, valueSchema="featurePhrases")
    add("catalogFeatureNegationPhrases", "Expressões que negam uma característica", json.dumps(["não é {feature}", "não tem {feature}", "sem {feature}", "não quero {feature}"], ensure_ascii=False), "textarea", maxLength=6000, valueSchema="featurePhrases")
    add("catalogTechnicalDetailLimit", "Máximo de fichas técnicas por consulta", 8, "integer", min=1, max=40)
    add("catalogTechnicalSearchLimit", "Buscas adicionais por característica", 2, "integer", min=0, max=6)
    add("catalogTechnicalConcurrency", "Consultas de ficha simultâneas", 2, "integer", min=1, max=5)
    add("critiqueUnavailableAction", "Conduta quando o revisor não pode executar", "grounded_fallback", "select", options=[{"value":"grounded_fallback","label":"Resposta com fatos validados"},{"value":"handoff","label":"Encaminhar para revisão humana"}])
    add("catalogReserveReviewCall", "Reservar chamada para revisão do catálogo", True, "boolean")
    messages = {
        "critique_handoff": ("Encaminhamento por falha de revisão", "Vou solicitar ajuda da equipe para continuar seu atendimento com os critérios que você informou."),
        "catalog_requirements_unknown": ("Ficha técnica sem confirmação", "Consultei as opções, mas ainda não consegui confirmar todos os critérios: {criteria}. Posso continuar a busca mantendo essas exigências."),
        "catalog_requirements_no_match": ("Opções consultadas incompatíveis", "As opções que consultei não atenderam a todos os critérios: {criteria}. Posso continuar procurando com essas características."),
        "catalog_requirements_intro": ("Introdução das opções confirmadas", "Confirmei estas opções com {criteria}:"),
        "catalog_required_budget": ("Descrição do orçamento exigido", "até R$ {amount}"),
        "catalog_requirement_line": ("Descrição de característica confirmada", "{label}"),
        "catalog_selection_missing": ("Escolha sem opções efetivamente apresentadas", "Ainda não apresentei uma opção confirmada com os seus critérios. Vou retomar essa busca antes de avançar."),
        "catalog_rerank_system": ("Instrução para ordenar candidatos", "Ordene os produtos de CANDIDATES conforme PREFERENCES. Retorne até {limit} IDs que constem em ALLOWED_PRODUCT_IDS. Use exclusivamente as características documentadas dos candidatos. Respeite todas as exigências técnicas e o orçamento; não altere fatos nem invente IDs."),
    }
    tree = ast.parse((ROOT / "app/verify/response_critique.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CRITIQUE_JUDGE_SYSTEM_PROMPT" for t in node.targets):
            messages["critique_system"] = ("Instrução da revisão de resposta", ast.literal_eval(node.value))
    for name, (label, default) in messages.items():
        fields.append(dict(key="message."+name, attribute=name, target="message", label=label, default=default,
                           type="textarea", maxLength=12000, group="Qualidade e critérios técnicos",
                           description="Texto editável no banco; preserve as variáveis indicadas."))
    existing = {d["key"] for d in definitions}
    definitions.extend(f for f in fields if f["key"] not in existing)
    path.write_text(json.dumps(definitions, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"definitions":len(definitions),"quality_keys":[f["key"] for f in fields]}))


if __name__ == "__main__":
    main()
