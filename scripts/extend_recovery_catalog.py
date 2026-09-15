"""Append versioned recovery controls to the migration seed (never a runtime fallback)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    path = ROOT / "sql/seeds/operator_catalog.json"
    definitions = json.loads(path.read_text(encoding="utf-8"))
    fields = [
        {"key": "catalogWarmBrands", "type": "textarea", "default": "Orient\nSeiko\nCitizen\nTissot\nBulova\nCasio\nLongines\nCertina\nTAG Heuer\nHamilton", "label": "Marcas a incluir na atualização do índice", "maxLength": 6000},
        {"key": "catalogWarmBrandLimit", "type": "integer", "default": 8, "min": 1, "max": 25, "label": "Marcas por execução de atualização"},
        {"key": "catalogWarmProductsPerBrand", "type": "integer", "default": 80, "min": 5, "max": 200, "label": "Produtos por marca na atualização"},
        {"key": "catalogStaleDays", "type": "integer", "default": 3, "min": 1, "max": 30, "label": "Dias para sinalizar dados antigos no catálogo"},
        {"key": "conversationRepairPhrases", "type": "textarea", "default": "que pergunta de preço\nnão perguntei o preço\nnão pedi preço\nnão foi isso que perguntei\nnão foi isso que pedi\nvocê não entendeu\nvocê entendeu errado\nta entendendo nada\ntá entendendo nada\nnão está entendendo\nnão ta entendendo\nnão tá entendendo", "label": "Expressões de correção e frustração", "maxLength": 6000},
        {"key": "conversationRepairHandoffAfter", "type": "integer", "default": 2, "min": 1, "max": 5, "label": "Reclamações consecutivas antes de encaminhar"},
        {"key": "checkoutContextMaxAgeSeconds", "type": "integer", "default": 43200, "min": 300, "max": 604800, "label": "Validade do contexto de carrinho sem pedido (segundos)"},
        {"key": "browseContextMaxAgeSeconds", "type": "integer", "default": 43200, "min": 300, "max": 604800, "label": "Validade da seleção de produtos (segundos)"},
    ]
    for field in fields:
        field.update(target="policy", attribute=field["key"], group="Continuidade e recuperação",
                     description="Política versionada aplicada a cada atendimento.")
    messages = {
        "double_check_system": ("Instrução da revisão independente", "Você é um juiz independente do agente de vendas. Não reescreva a resposta. Não sugira APIs. action=approve se a reply responde o pedido com os fatos listados. action=veto se inventou preço, link ou SKU, ignorou PIX/pedido existente, ou não atendeu o pedido. action=handoff só se pagamento ou pedido foi afirmado sem evidência. code deve ser pix, price, order, sku ou unanswered. Diferencie uma reclamação sobre a resposta anterior de uma solicitação comercial. requested_subject descreve o desejo do cliente, não um produto confirmado no catálogo: repetir esse desejo para reconhecer uma correção não afirma estoque, preço ou existência. Somente products e os campos de pedido/pagamento fornecidos sustentam fatos comerciais."),
        "double_check_insufficiency": ("Resposta quando faltam evidências", "Não consigo confirmar isso com segurança agora. Posso verificar de novo ou te passar para um atendente."),
        "conversation_repair_ack": ("Reconhecimento de erro na interpretação", "Você tem razão, interpretei seu pedido errado. Vou retomar os critérios que você informou."),
        "conversation_repair_handoff": ("Encaminhamento após falha de compreensão", "Desculpe pela confusão. Vou solicitar ajuda da equipe para continuar seu atendimento com as informações que você já passou."),
        "conversation_repair_missing_context": ("Correção sem contexto suficiente", "Desculpe, interpretei errado. Qual ponto da resposta você quer que eu corrija?"),
    }
    for key, (label, default) in messages.items():
        fields.append({"key": "message." + key, "target": "message", "attribute": key,
                       "label": label, "default": default, "group": "Continuidade e recuperação",
                       "description": "Mensagem ou instrução de atendimento editável e versionada.",
                       "type": "textarea", "maxLength": 6000, "variables": []})
    existing = {field["key"] for field in definitions}
    definitions += [field for field in fields if field["key"] not in existing]
    path.write_text(json.dumps(definitions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"fields": len(definitions), "recovery_fields": len(fields)}))

if __name__ == "__main__":
    main()
