"""Move legacy business constants into the versioned database catalog."""
import ast
import json
from migrate_operator_copy import ROOT, SEED, apply_edits, add_import, call_for

fields = json.loads(SEED.read_text(encoding="utf-8"))
constants = set()


def policy_field(key, value, label, *, value_schema=None):
    fields.append({"key": key, "target": "policy", "attribute": key,
        "default": value, "label": label, "description": "Configuração comercial versionada do workspace.",
        "group": "Políticas comerciais", "type": "textarea" if value_schema else "text",
        "maxLength": 30000, **({"valueSchema": value_schema} if value_schema else {})})


site_path = ROOT / "app/persona/site_knowledge.py"
site_tree = ast.parse(site_path.read_text(encoding="utf-8"))
site_values = {}
edits = []
for node in site_tree.body:
    if not isinstance(node, ast.Assign):
        continue
    name = node.targets[0].id
    constants.add(name)
    if name in {"SITE_URL", "STORE_URL", "STORE_PRONTA_ENTREGA_URL", "NS_SALES_WHATSAPP"}:
        value = ast.literal_eval(node.value)
        site_values[name] = value
        key = "business." + name.lower()
        policy_field(key, value, name.replace("_", " ").capitalize(), value_schema=None)
        body = f"policy({key!r})"
    elif name == "CARD_USAGE_TABLE":
        value = ast.literal_eval(node.value)
        policy_field("business.credit_bands", json.dumps(value), "Faixas do Cartão Presente (centavos)", value_schema="creditBands")
        body = "tuple(tuple(band) for band in json.loads(policy('business.credit_bands')))"
    elif name == "TRADE_IN_HANDOFF_MESSAGE":
        body = "operator_message('trade_in_handoff' if policy('acceptsTradeIn') else 'trade_in_unavailable')"
    elif name == "HUMAN_HANDOFF_ACK_MESSAGE":
        body = "operator_message('handoff_requested')"
    else:
        body = call_for(fields, node.value, str(site_path.relative_to(ROOT))+":"+str(node.lineno), "business."+name.lower())
    assert body
    edits.append((node, f"def {name}():\n    return {body}"))
apply_edits(site_path, edits)
add_import(site_path)
source = site_path.read_text(encoding="utf-8")
source = source.replace("from __future__ import annotations", "from __future__ import annotations\n\nimport json\nfrom app.configuration.runtime import policy")
site_path.write_text(source, encoding="utf-8")

# The entire knowledge text remains operator-authored; replace the contradictory
# trade paragraph with the shared structured policy at render time.
source = site_path.read_text(encoding="utf-8")
begin = source.index("Política comercial New Store Relógios:")
end = source.index("FAQ resumido:", begin)
source = source[:begin] + "Política de permuta vigente:\n{TRADE_IN_HANDOFF_MESSAGE}\n\n" + source[end:]
site_path.write_text(source, encoding="utf-8")
tree = ast.parse(source)
edits = []
for function in tree.body:
    if isinstance(function, ast.FunctionDef) and function.name in {"build_site_knowledge_text", "build_rules_reply", "format_card_usage_table_text"}:
        for node in ast.walk(function):
            if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value.split()) >= 5):
                # Only top-level string expressions; never f-string children/docstrings.
                parents = {child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)}
                if isinstance(parents.get(node), (ast.JoinedStr, ast.Expr, ast.FormattedValue)):
                    continue
                body = call_for(fields, node, str(site_path.relative_to(ROOT))+":"+str(node.lineno), "business."+function.name)
                if body:
                    edits.append((node, body))
apply_edits(site_path, edits)

for relative, names in {
    "app/agents/door.py": {"PERSONA_GREETING_OPERATIONAL", "SYSTEM_INSTRUCTIONS", "STORE_LOOKUP_UNAVAILABLE", "GENERAL_GREETING_FALLBACK", "STORE_KNOWLEDGE_UNAVAILABLE"},
    "app/identity/greeting_policy.py": {"GREETING_REPLY"},
}.items():
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))
    edits = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name) or node.targets[0].id not in names:
            continue
        name = node.targets[0].id
        constants.add(name)
        value = node.value
        stripped = isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "strip"
        if stripped:
            value = value.func.value
        body = call_for(fields, value, relative+":"+str(node.lineno), "business."+name.lower())
        assert body
        edits.append((node, f"def {name}():\n    return {body}" + (".strip()" if stripped else "")))
    apply_edits(path, edits)
    add_import(path)

# Greeting variants and institutional snippets are structured, editable content.
path = ROOT / "app/identity/greeting_policy.py"
tree = ast.parse(path.read_text(encoding="utf-8"))
node = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "_FALLBACK_GREETING_VARIANTS")
variants = [e.value if isinstance(e, ast.Constant) else "Olá! Como posso ajudar?" for e in node.value.elts]
variants = [v.replace("Crono da New Store Relógios", "{agent_name}").replace("Crono", "{agent_name}") for v in variants]
policy_field("business.greeting_variants", json.dumps(variants, ensure_ascii=False), "Variações de saudação", value_schema="greetingVariants")
apply_edits(path, [(node, "def _FALLBACK_GREETING_VARIANTS():\n    import json\n    from app.configuration.runtime import policy\n    from app.persona.persona_runtime import get_persona_runtime\n    runtime = get_persona_runtime()\n    name = runtime.agent_display_name if runtime else policy('business.agent_name')\n    return tuple(v.format(agent_name=name) for v in json.loads(policy('business.greeting_variants')))" )])
constants.add("_FALLBACK_GREETING_VARIANTS")
policy_field("business.agent_name", "Crono", "Nome do agente quando não há perfil ativo")

path = ROOT / "app/persona/store_knowledge.py"
tree = ast.parse(path.read_text(encoding="utf-8"))
node = next(n for n in tree.body if isinstance(n, ast.AnnAssign) and n.target.id == "_INSTITUTIONAL_SNIPPETS")
snippets = eval(compile(ast.Expression(node.value), "<migration>", "eval"), {"STORE_URL": site_values["STORE_URL"]})
snippets = list(snippets)
snippets[0]["policyKey"] = "acceptsTradeIn"
policy_field("business.institutional_knowledge", json.dumps(snippets, ensure_ascii=False), "Conhecimento institucional e termos de busca", value_schema="institutionalKnowledge")
apply_edits(path, [(node, "def _INSTITUTIONAL_SNIPPETS():\n    import json\n    from app.configuration.runtime import policy\n    entries = json.loads(policy('business.institutional_knowledge'))\n    for entry in entries:\n        if entry.get('policyKey') == 'acceptsTradeIn':\n            entry['body'] = trade_in_policy_text()\n    return entries")])
constants.add("_INSTITUTIONAL_SNIPPETS")

# Imports retain their names; values are now resolved in the active workspace at
# call time. Update consumers, including tests asserting the published fixture.
for folder in ("app", "tests"):
    for path in (ROOT / folder).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        edits = []
        for node in ast.walk(tree):
            match = isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in constants
            match |= isinstance(node, ast.Attribute) and node.attr in constants and isinstance(node.value, ast.Name) and node.value.id in {"door", "site_knowledge", "greeting_policy"}
            if not match:
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            edits.append((node, ast.unparse(node)+"()"))
        if edits:
            apply_edits(path, edits)
SEED.write_text(json.dumps(fields, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
print(json.dumps({"dynamic_constants": len(constants), "catalog_fields": len(fields)}))
