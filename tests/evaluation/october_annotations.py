"""Intent annotations by this Codex session, not predictions from the deployed model.

Only inputs/history informed these annotations. No expected answer is returned by
the replay model boundary. Missing original media remains explicitly unresolved.
"""
from copy import deepcopy


def annotation(intent, goal, **fields):
    return {"primary_intent": intent, "user_goal": goal, "confidence": 0.95,
            "answer_strategy": "answer_directly", **fields}


ANNOTATIONS = {}
for case_id in ("1128", "1129", "1131", "1132", "1134", "1139", "1142", "1165", "1168", "1181"):
    ANNOTATIONS[case_id] = annotation("greeting", "encerramento" if case_id in {"1128", "1129", "1165"} else "saudação", answer_strategy="acknowledge")

for case_id in ("1127", "1130", "1143", "1166", "1167", "1169", "1179"):
    ANNOTATIONS[case_id] = annotation("commerce_inspect", "identificar relógio e consultar preço/disponibilidade da mídia",
        references_previous_context=True, confidence=0.5, answer_strategy="clarify",
        clarification_required=True, clarification_reason="original_visual_evidence_missing",
        clarification_question="Você consegue indicar o modelo ou enviar uma foto do relógio?",
        references=[{"kind": "current_product"}])

for case_id, goal in {
    "1133": "consultar status do pedido", "1138": "consultar previsão de postagem",
    "1141": "previsão cadastrada já venceu", "1147": "informações logísticas do pedido",
    "1151": "confirmar pagamento já realizado", "1135": "dúvida sobre pedido",
    "1136": "consultar sem número do pedido", "1137": "CPF como alternativa para consultar pedido",
    "1145": "consultar pedido 25696", "1172": "consultar meu pedido",
    "1173": "CPF para consultar pedidos do titular", "1174": "CPF sem finalidade explícita",
    "1175": "confirmar tipo CPF", "1176": "consultar meu pedido",
    "1177": "reclamar do atraso do pedido", "1178": "recusar encaminhamento",
    "1182": "consultar compra realizada", "1183": "CPF para consultar compra",
    "1184": "documento para localizar compra", "1187": "compra em atendimento humano",
}.items():
    action = {"kind": "get_order_status"}
    if case_id == "1133": action["order_id"] = "26052"
    if case_id == "1145": action["order_id"] = "25696"
    if case_id == "1151": action["kind"] = "order_payment"
    if case_id == "1178": action = {"kind": "none", "confirmation": "reject"}
    ANNOTATIONS[case_id] = annotation("commerce_after_sales", goal, references_previous_context=True,
                                      requested_action=action, purchase_stage="after_sales")

ANNOTATIONS["1140"] = annotation("store_general", "pedido aberto de informação", answer_strategy="clarify",
    clarification_required=True, clarification_question="Sobre qual assunto você quer informação?")
for case_id in ("1174", "1175"):
    # A document by itself is not authorization or intent to query an order.
    ANNOTATIONS[case_id] = annotation("store_general", "confirmar finalidade do CPF informado",
        answer_strategy="clarify", clarification_required=True,
        clarification_question="Você quer consultar um pedido com esse CPF?",
        requested_action={"kind": "none"}, references_previous_context=case_id == "1175")
ANNOTATIONS["1144"] = annotation("commerce_find", "identificar modelo Traska no mesmo assunto visual",
    entities={"brand": "Traska"}, soft_preferences={"brand": "Traska"}, references_previous_context=True,
    answer_strategy="search_catalog", required_tools=["search_products"])
ANNOTATIONS["1146"] = annotation("commerce_find", "refinar Traska com material aço",
    soft_preferences={"material": "aço"}, references_previous_context=True,
    answer_strategy="search_catalog", required_tools=["search_products"])

for case_id in ("1148", "1149", "1150", "1152", "1153", "1154", "1156", "1157", "1158", "1163", "1164"):
    prefs = {"availability": "pronta entrega"} if case_id != "1163" else {"style": "dress"}
    if case_id in {"1150", "1154"}: prefs.update(occasion="casamento")
    if case_id == "1150": prefs["urgency"] = "próximo final de semana"
    if case_id == "1156": prefs["recipient"] = "uso próprio"
    fields = dict(entities={"category": "relógio"}, soft_preferences=prefs, references_previous_context=case_id not in {"1148", "1163"},
                  answer_strategy="search_catalog", required_tools=["search_products"])
    if case_id in {"1152", "1154"}: fields["conversation_feedback"] = "repeated_question"
    if case_id in {"1153", "1158"}: fields["conversation_feedback"] = "frustrated"
    ANNOTATIONS[case_id] = annotation("commerce_recommend", "consultar lista de relógios disponíveis para o cliente", **fields)

for case_id in ("1155", "1159", "1160"):
    fields = dict(references_previous_context=True, requested_action={"kind": "inspect"},
                  active_topic="disponibilidade, prazo de postagem e entrega")
    if case_id == "1155":
        fields.update(entities={"brand": "Certina", "model": "DS Action Day Date", "reference": "C032.430.11.091.00"},
                      hard_constraints={"reference": "C032.430.11.091.00"}, soft_preferences={"color": "verde"},
                      required_tools=["search_products", "get_stock"], answer_strategy="search_catalog")
    if case_id == "1160": fields["conversation_feedback"] = "misunderstood"
    ANNOTATIONS[case_id] = annotation("commerce_inspect", "distinguir disponibilidade, envio e chegada do produto", **fields)

ANNOTATIONS["1161"] = annotation("commerce_discover", "acessar catálogo completo da loja", answer_strategy="search_catalog", required_tools=["search_products"])
ANNOTATIONS["1162"] = annotation("commerce_after_sales", "usar CPF como alternativa de identificação", references_previous_context=True)
ANNOTATIONS["1170"] = annotation("commerce_inspect", "verificar disponibilidade do Seiko SSA459J1",
    entities={"brand": "Seiko", "model": "Presage Cocktail Midnight Mockingbird", "reference": "SSA459J1"},
    hard_constraints={"reference": "SSA459J1"}, requested_action={"kind": "inspect"}, active_topic="disponibilidade",
    required_tools=["search_products", "get_stock"], answer_strategy="search_catalog")
ANNOTATIONS["1171"] = annotation("out_of_scope", "proposta de fornecedor de design e marketing", answer_strategy="refuse")
ANNOTATIONS["1180"] = annotation("commerce_inspect", "cor aproximada da pulseira do Frederique Constant mencionado anteriormente",
    references_previous_context=True, active_topic="cor da pulseira", answer_strategy="clarify",
    clarification_required=True, clarification_reason="old_thread_requires_confirmation",
    clarification_question="Você está falando da pulseira do Frederique Constant que conversamos antes?")
for case_id in ("1185", "1186"):
    ANNOTATIONS[case_id] = annotation("human_handoff", "falar com atendente", answer_strategy="handoff",
        requested_action={"kind": "handoff", "confirmation": "confirm"}, references_previous_context=True)


def get_annotation(case_id):
    return deepcopy(ANNOTATIONS[str(case_id)])
