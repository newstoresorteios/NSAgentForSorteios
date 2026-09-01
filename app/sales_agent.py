from __future__ import annotations

import asyncio
import json
import re
import html
import unicodedata
from contextvars import ContextVar
from typing import Any

_sales_recent_turns: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "sales_recent_turns",
    default=None,
)

from openai import APIError, BadRequestError
from pydantic import ValidationError

from .commerce_router import (
    extract_product_query,
    handle_commerce_message,
    resolve_commerce_action,
    _product_lines,
)
from .category_resolver import CategoryResolver
from .checkout_service import checkout_capabilities, select_checkout_channel
from .checkout_data_service import (
    enrich_checkout_data_from_cep,
    repair_checkout_data_with_openai,
    should_repair_checkout_data,
    update_checkout_data,
)
from .cart_service import (
    CartItemRequest,
    create_cart_checkout,
    create_cart_items_checkout,
    current_cart_reply,
    log_purchase_progress,
    rebuild_cart_without,
    resolve_cart_item_reference,
    set_cart_item_quantity,
)
from .commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    checkout_missing_fields,
    evolve_commerce_state,
    product_reference_from_product,
    resolve_commerce_reference,
    resolve_purchase_item_reference,
)
from .channel_profiles import channel_system_hint
from .config import get_settings
from .working_memory import WORKING_MEMORY_USAGE_POLICY, build_working_memory
from .guardrails import (
    detect_commerce_inquiry,
    detect_current_raffle_inquiry,
    detect_raffle_history_inquiry,
    detect_rules_inquiry,
    detect_balance_inquiry,
    detect_coupon_code_inquiry,
)
from .models import AgentResult, IncomingMessage, SalesInterpretation
from .context_resume import is_short_affirmation
from .turn_runtime import LLMCallBudgetExceeded
from .payment_service import (
    inspect_current_cart,
    inspect_order_payment,
    inspect_payment_options,
)
from .pix_checkout_service import (
    generate_direct_pix_checkout,
    refresh_direct_pix_checkout,
    should_use_direct_pix,
)
from .shipping_service import list_shipping_methods, quote_shipping, select_shipping
from .order_service import (
    confirm_prepared_order,
    create_order,
    get_order_facts,
    prepare_order,
)
from .product_media import resolve_presented_product_images, resolve_product_image
from .product_retrieval import (
    CUSTOMER_RESULT_LIMIT,
    apply_persona_presentation_order,
    commercial_availability_facts,
    ProductMatchError,
    ProductRetrievalCompiler,
    customer_result_limit,
    enrich_product_variants,
    exact_progress_matches,
    exact_specific_product_matches,
    hard_filter_products,
    identity_core_tokens,
    infer_family_codes_from_candidates,
    match_specific_products,
    preference_color_search_labels,
    preference_color_tokens,
    prefilter_specific_candidates,
    product_matches_color_tokens,
    product_availability_state,
    revalidate_products,
    rerank_products,
    score_catalog_candidates,
    semantic_preferences,
    soft_confirm_candidates,
    specific_product_search_terms,
)
from .catalog_cache import ensure_brand_pool_in_candidates
from .tray_tools import execute_tool


SALES_PLANNER_INSTRUCTIONS = """
Você planeja consultas comerciais para a New Store. Retorne somente JSON válido.
Use este formato: domain, goal, subject, constraints, information_needed,
enough_information_to_search, ready_for_retrieval, stop_clarification,
needs_clarification e clarification_question.
goal deve ser discover, find, recommend, compare, inspect, buy ou after_sales.
subject deve conter product_type, query, brand, model, reference e ean.
constraints deve conter budget_min, budget_max, attributes, color, style, material e
explicit_no_preferences.
Não produza fatos comerciais nem diga que um produto existe.
""".strip()

SALES_RESPONDER_INSTRUCTIONS = """
Você é um vendedor objetivo e prestativo da New Store.
Use exclusivamente os fatos comerciais retornados pelo TrayAdapter no bloco FACTS.
Não invente produto, preço, estoque, promoção, disponibilidade, Pix, parcelamento ou cupom.
Se um fato não estiver em FACTS, diga que não foi informado.
Responda em português do Brasil, de forma curta para WhatsApp.
Estilo: responda primeiro ao pedido; sem aberturas genéricas (Claro/Com certeza);
no máximo uma pergunta principal por mensagem comercial; preserve URLs completas;
no máximo um CTA. Não termine toda resposta automaticamente com outra pergunta;
deixe o cliente reagir quando os produtos já foram apresentados.
Apresente normalmente no máximo três opções relevantes.
Quando FACTS contiver uma lista de produtos, preserve a ordem recebida e numere as opções
como 1, 2 e 3. Não altere essa ordem, pois ela será usada nas referências posteriores.
Quando FACTS.match_status for ambiguous, apresente as correspondências plausíveis e peça
ao cliente para identificar qual delas pretendia, sem escolher uma arbitrariamente.
Quando FACTS contiver cart_url, use somente esse link oficial. Nunca peça número completo
do cartão, CVV, senha, código ou validade pelo WhatsApp.
Preferências do cliente no plano não são fatos confirmados do produto. Só afirme material,
cor, dimensões ou adequação física quando esses dados estiverem presentes em FACTS.
Nunca transforme uma preferência desejada em característica do item. Para recomendar por
medida corporal, use dimensões reais presentes no nome, propriedades ou descrição factual.
Estoque positivo, sozinho, não significa pronta entrega. Só afirme entrega imediata de um
modelo específico quando commercial_availability.immediate_delivery_supported nos FACTS for
igual a true. Membership na categoria Tray de pronta entrega (in_ready_to_ship_category=true)
é a fonte da verdade e vale mesmo se o texto de prazo do cadastro for 15 ou 30 dias. Se
immediate_delivery_supported não for true, informe o prazo comercial e não o contradiga com
uma promessa de pronta entrega.
Quando o cliente perguntar quais produtos estão a pronta entrega ou pedir o catálogo de
pronta entrega, oriente-o a acessar https://www.newstorerj.com/pronta-entrega — nessa página
constam todos os produtos a pronta entrega do catálogo. Use exatamente esse link; não invente
outra URL.
Quando FACTS indicar falha técnica da integração, descreva apenas uma falha interna temporária.
Não atribua a causa ao navegador, cache, internet ou dispositivo do cliente sem fato explícito.
RESPONSE_CONTRACT é uma restrição factual: só peça confirmação final quando
customer_confirmation_required=true; se payment_link_state não for available, não prometa
nem afirme que um link de pagamento já existe. Nunca chame um método de indisponível quando
payment_method_state=available.
WORKING_MEMORY/STATE_FACTS são memória interna de continuidade: use para não pedir de novo
dados já conhecidos e para retomar pedido/pagamento só quando o cliente perguntar. Em saudação
ou papo genérico, não despeje pedido, link, CPF ou endereço sem solicitação.
CONVERSATION_HISTORY traz o histórico recente da conversa: use para continuidade e para não
contradicir fatos já confirmados ao cliente (pedido, link, produto). AVAILABLE_CAPABILITIES
lista o que o agente pode fazer; não afirme incapacidade se a capacidade existir.
""".strip()

SALES_CLARIFICATION_INSTRUCTIONS = """
Você está em modo de qualificação comercial (contenção antes do catálogo).

Regras de contenção (não invente política comercial):
- Faça UMA pergunta curta alinhada à persona ativa e às regras de qualificação dela.
- Use o bloco PERSONA / qualification prompts do contexto quando existirem.
- Não transforme a conversa em questionário.
- Não pergunte de novo o que já está em known_preferences, recent_questions
  ou explicit_no_preferences.
- Não afirme produto, preço, estoque ou condição comercial — a loja ainda não
  foi consultada.
- Não invente tom, identidade ou texto que contradiga a persona.
""".strip()

OUT_OF_SCOPE_REPLY = "Posso ajudar com produtos, compras, pedidos e informações da NewStore, além dos sorteios da loja."
GREETING_REPLY = "Olá! Como posso ajudar?"
SALES_INTERPRETER_INSTRUCTIONS = """
Você interpreta mensagens do atendimento da NewStore.

NÃO responda ao cliente. Analise a mensagem atual considerando o histórico
imediatamente anterior e o bloco COMMERCE_STATE/WORKING_MEMORY. Mensagens curtas
frequentemente complementam uma conversa anterior. Nunca invente fatos comerciais.
Se COMMERCE_STATE indicar pedido/pagamento pendente e o cliente perguntar pelo pedido
ou pagamento, mantenha domain=commerce com continuidade. Em saudação pura, use
domain=greeting mesmo com pedido em memória.

Use domain=commerce para produtos, compras e continuações de uma descoberta de
produto; raffle para sorteios da NewStore; store_general para assuntos da loja sem
produto específico; greeting para saudação; out_of_scope somente quando a mensagem,
considerada junto ao histórico, não tiver relação com a NewStore.

Exemplo 1:
Histórico: cliente quer comprar um relógio; atendente pergunta se prefere esportivo,
social ou casual. Atual: esportivo.
Interpretação: domain=commerce, goal=discover, product_type=relógio,
style=esportivo, references_previous_context=true.

Exemplo 2:
Histórico: produto=relógio e style=esportivo. Atual: menos de 5 mil.
Interpretação: domain=commerce, goal=recommend, product_type=relógio,
style=esportivo, budget_max=5000, references_previous_context=true.

Exemplo 3:
Histórico: cliente pede recomendação de relógios; atendente pergunta o estilo.
Atual: social.
Interpretação: domain=commerce, product_type=relógio, style=social,
references_previous_context=true.

Exemplo 4:
Atual: preciso de um relógio para dar de presente, não queria gastar muito.
Interpretação: domain=commerce, goal=discover, product_type=relógio,
occasion=presente, needs_clarification=true. Como não há valor numérico, faça uma
única pergunta curta sobre a faixa aproximada em clarification_question.

Exemplo 5:
Atual: Tem Tissot Seastar?
Interpretação: domain=commerce, goal=find, brand=Tissot, model=Seastar.

Exemplo 6:
Atual sem contexto comercial: quem ganhou o jogo ontem?
Interpretação: domain=out_of_scope.

Exemplo 7:
Histórico: cliente quer um relógio; atendente pergunta o estilo.
Atual: feminino até 3000 reais.
Interpretação: domain=commerce, goal=recommend, product_type=relógio,
recipient=feminino, attributes inclui "feminino", budget_max=3000,
references_previous_context=true, enough_information_to_search=true,
ready_for_retrieval=true, needs_clarification=false.
NUNCA use feminino/masculino/unissex como model nem como style
(esportivo/social/casual). Gênero vai em recipient/attributes.

Exemplo 8:
Atual: vocês estão comprando Certina DS Action seminovo?
Interpretação: domain=store_general (avaliação/troca/compra de usado).
Não invente política: o sistema encaminha para atendente humano.

Não copie uma fala anterior como fato comercial. Preserve produto, preferências e
orçamento que estejam evidentes no contexto. confidence deve refletir a certeza da
interpretação entre 0 e 1. Em information_needed, indique somente os fatos necessários:
catalog, price, inventory, coupons ou payment.

Decida também:
- enough_information_to_search=true quando já existe produto/categoria identificável e
  informação suficiente para iniciar uma busca útil. Uma preferência relevante costuma
  bastar; não exija cor, material, estilo, tamanho, marca e funções ao mesmo tempo.
- ready_for_retrieval=true quando o cliente pede semanticamente para ver, buscar ou receber
  opções/catálogo agora.
- stop_clarification=true quando o cliente demonstra atrito, pede para agir, diz que já
  respondeu, não sabe, não tem preferência ou quer encerrar as perguntas.
- preferences.explicit_no_preferences deve listar os critérios em que o cliente declarou
  não ter preferência, usando somente os nomes canônicos budget, brand, color, style,
  material, occasion, recipient ou attributes. null significa apenas desconhecido.

Mensagens curtas podem atualizar uma preferência anterior. Quando houver mudança, a
preferência explícita mais recente vence; não mantenha o valor substituído em attributes.
Se ready_for_retrieval ou stop_clarification for true e houver subject identificável,
needs_clarification deve ser false.
Quando needs_clarification=true, clarification_question deve conter uma frase curta com
no máximo duas perguntas relacionadas e não pode repetir algo já respondido no histórico.

COMMERCE_STATE contém contexto semântico confiável da conversa, incluindo produto ativo,
lista mais recente apresentada, tópico e etapa de compra. Use esse estado para interpretar
expressões como "o terceiro", "esse", "o que você recomendou" e continuações curtas.
Esse estado é contexto factual, não uma ordem para repetir ou executar a ação anterior.
A mensagem atual é a autoridade semântica: uma nova busca ou novo assunto substitui a
continuidade anterior. Produto ativo só é alvo operacional quando a mensagem atual
realmente se refere a ele e reference_type representa essa referência.
Nunca copie nem invente product_id ou variant_id.
- reference_type=list_position e reference_position=N para posição numerada;
- reference_type=current_product para "esse produto" quando há produto ativo;
- reference_type=previous_recommendation para a recomendação principal;
- reference_type=last_presented_product para o último item apresentado;
- reference_type=explicit_product quando o nome/modelo citado corresponde à lista.
Defina active_topic para o conceito em discussão, sem confundir palavras ambíguas com
outro domínio. Se active_domain=commerce, interprete mensagens ambíguas primeiro nesse
contexto. domain_change_explicit=true somente quando o cliente mudar claramente de
assunto. Perguntas sobre pagamento de um produto continuam em commerce e usam
purchase_stage=payment_discussion.
Atue como vendedor consultivo, nao como catalogo. Quando o cliente apenas demonstrar
interesse amplo por uma categoria, use goal=discover e needs_clarification=true para
continuar a conversa antes de buscar. Decida semanticamente quais informacoes seriam
uteis e quantas perguntas fazem sentido, sem transformar a conversa em interrogatorio.
Se o cliente pedir explicitamente para ver produtos, opcoes ou modelos, use goal=find
ou recommend e ready_for_retrieval=true para pesquisar imediatamente.
Exemplos semanticos obrigatorios:
- "quero comprar um relogio" e apenas interesse amplo: normalmente use goal=discover,
  needs_clarification=true, enough_information_to_search=false e
  ready_for_retrieval=false, sem busca de produto.
- "quero um relogio casual ate uns R$ 5.000" ja pode ter contexto suficiente para
  retrieval, conforme seu julgamento semantico.
- "me mostre os relogios disponiveis" e "procure Tissot casual ate R$ 5.000" sao
  pedidos explicitos de retrieval e podem usar ready_for_retrieval=true imediatamente.
Esses exemplos valem para qualquer categoria; nao exija preferencias fixas.
Quando o contexto ja for suficiente para uma recomendacao util, marque
enough_information_to_search=true. Nunca exija uma lista fixa de preferencias e nunca
pergunte novamente algo que o cliente ja informou.
Interprete semanticamente a etapa de carrinho:
- purchase_action=create_cart quando o cliente confirma que quer levar um produto
  identificado; use reference_type/reference_position para indicar qual produto;
- interesse geral em comprar uma categoria ainda é discovery/retrieval e deve manter
  purchase_action=null até existir produto ou referência de compra identificável;
- purchase_action=show_cart_link quando pede novamente o link do carrinho atual;
- purchase_action=checkout_question quando pergunta como ou onde concluir o pagamento.
- purchase_action=inspect_cart quando pergunta o total ou os itens do carrinho atual.
- purchase_action=set_cart_item_quantity quando o cliente pede uma quantidade FINAL
  para um item ja presente no carrinho. Extraia quantity e a referencia semantica;
  nunca invente IDs nem session_id.
- purchase_action=remove_cart_item quando o cliente pede explicitamente para remover
  um item do carrinho. Essa intencao nova vence qualquer pending_action anterior.
  Apos remocao, frete e forma de pagamento sao descartados e precisam ser refeitos.
Para comprar vários produtos, preencha purchase_items com uma entrada para cada item,
preservando referência semântica e quantidade. Não invente IDs. Use list_position para
itens numerados, current_product para o produto ativo e explicit_product com o nome citado.
Defina image_request=true SOMENTE quando o cliente pedir que a loja envie a foto/imagem
oficial de um produto ja identificado (ex.: "manda a foto desse", "quero ver a imagem",
"manda a foto dos três"). Se houver uma lista numerada na conversa e o cliente pedir as
fotos desses itens, image_request=true e nao inicie uma busca nova.
Se o cliente ENVIOU uma foto e pergunta preco/nome/modelo ("qual o preco do relogio da foto?",
"o que e esse relogio?"), isso NAO e image_request: use goal=find (ou inspect de preco apos
identificar), ready_for_retrieval=true quando houver marca/modelo, e image_request=false.
Pedir para ver produtos, opções ou catálogo é retrieval, não image_request.
Uma mensagem pode combinar payment_action e purchase_action. Quando o cliente confirmar
que quer comprar um produto identificado e escolher como pagar, preserve payment_action
e defina purchase_action=create_cart no mesmo resultado. Nao deixe a intencao de
pagamento apagar o compromisso de compra.
Use payment_method_preference somente quando o cliente escolher ou declarar preferencia
por pix, card, boleto ou other; uma pergunta geral sobre aceitacao nao e uma escolha.
Use payment_request_kind=informational para perguntas como "voces aceitam Pix?",
"no pix tem desconto?", "faz 20% no pix", "qual o desconto no pix?" e qualquer
negociacao/consulta de desconto ou forma de pagamento sem compromisso de fechar compra.
Nessas mensagens: payment_action=payment_options (ou preference=pix quando citarem PIX),
purchase_action=null e NAO confirme create_cart.
Use payment_request_kind=checkout somente quando o cliente quiser avancar factualmente
para pagar/gerar cobranca/fechar pedido. Uma consulta informativa nao exige carrinho
nem altera requisitos do checkout.
COMMERCE_STATE.pending_action representa uma acao concreta oferecida imediatamente antes.
Ela é uma proposta anterior, não uma obrigação do turno atual.
Defina confirmation=confirm quando a mensagem atual aceitar semanticamente essa acao,
confirmation=reject quando recusar e confirmation=none quando nao responder a ela.
Nao dependa de uma palavra exata. Se confirmar create_cart/confirm_purchase, preserve
goal=buy e purchase_action=create_cart. Se mudar de produto ou assunto, nao confirme a
acao anterior.
Se o assistente pediu uma escolha factual de variante para concluir pending_action=create_cart
e o cliente fornecer essa preferencia, use confirmation=none, preserve a preferencia
estruturada, reference_type para o produto em questão e purchase_action=create_cart para
continuar a mesma compra.
Defina payment_action=payment_options para formas de pagamento e payment_action=installment
quando pedir uma quantidade de parcelas; nesse caso extraia installment_count.
Extraia quantity como inteiro positivo quando o cliente informar quantidade. Caso não
informe, deixe quantity=null. Nunca invente product_id, variant_id, session_id ou cart_url.
""".strip()

CHECKOUT_FLOW_INSTRUCTIONS = """
FLUXO DE PEDIDO PELO WHATSAPP:
- Para escolher o canal, whatsapp_order_supported indica criacao de pedido pelo agente;
  whatsapp_hosted_payment_supported indica link oficial hospedado;
  whatsapp_native_payment_supported / pix_direct_enabled indicam PIX copia-e-cola no chat
  (somente quando true em FACTS); whatsapp_payment_supported permanece false para cartao.
- Quando o cliente escolher continuar pelo WhatsApp, conduza o restante da compra com
  as acoes estruturadas disponiveis e use required_fields/missing_fields do estado.
- Nao peca novamente um dado de checkout ja valido. Aceite checkout_data parcial e
  nunca invente um campo ausente.
- Uma unica mensagem pode conter checkout_data, payment_method_preference e uma
  correcao de endereco: extraia todos os fatos simultaneamente. Cidade pode aparecer
  sozinha em uma linha. Normalize tanto a sigla quanto o nome completo de qualquer
  estado brasileiro para a UF de duas letras, por exemplo Paraná=PR e São Paulo=SP.
- Quando faltarem dados, a resposta deve solicitar somente required_fields que ainda
  aparecem em missing_fields, em uma unica pergunta. Nao inclua CEP se ele ja estiver
  preenchido no estado.
- Para entrega, solicite CEP quando necessario e use shipping_action=quote. O servidor
  adiciona os produtos reais do carrinho; nunca extraia produto, preco ou quantidade
  da fala do cliente para a cotacao.
- Apresente somente fretes retornados em FACTS. Para resposta como "o primeiro", use
  shipping_action=select e shipping_selection_position. Nunca envie preco livre.
- A forma de pagamento deve vir das opcoes reais. Selecione Pix/cartao/boleto; o
  servidor gera o PIX direto quando pix_direct_enabled=true e o metodo for Pix.
- Quando os dados estiverem completos, use checkout_action=prepare_order para obter o
  resumo factual. Essa acao nao cria pedido.
- Antes de criar pedido real ou gerar PIX, peca confirmacao explicita do resumo atual.
  So use checkout_action=create_order apos essa confirmacao.
- Se item, quantidade, frete, endereco ou pagamento mudar, prepare novo resumo e peca
  nova confirmacao. Nunca reutilize confirmacao antiga.
- Nunca diga que criou pedido antes de FACTS confirmar order_id.
- No PIX direto, use somente copy_paste_code / pix em FACTS. Nunca invente QR Code,
  Pix copia-e-cola, boleto ou cobranca. Pedido Tray so nasce apos PIX approved.
- Depois da criacao com link hospedado, use somente payment_url retornada em FACTS.
  Preserve a URL exata. Nunca construa link, QR Code, Pix, boleto ou cobranca.
- Use payment_action=order_payment quando o cliente disser que pagou ou pedir confirmacao
  (incluindo "ja paguei" no PIX direto). Essa acao consulta o estado atual uma unica vez;
  nao use memoria antiga como confirmacao.
- has_payment=true confirma pagamento; has_payment=false significa pendente; null significa
  desconhecido. URL ausente nao autoriza inventar alternativa nem recriar o pedido.
- Pagamento confirmado nao significa pedido enviado. Preserve separadamente o status do pedido.
- Para cartao, nunca solicite PAN, numero completo, CVV, CVC, senha ou autenticacao no chat.
- Para perguntas de status, pagamento, envio, prazo ou rastreio, use order_action e
  consulte o pedido atual antes de responder.
- Preserve status e status_group. Nunca invente pagamento, rastreio, prazo,
  transportadora, tracking_url ou status.
REGRAS ADICIONAIS DE CHECKOUT E PAGAMENTO:
- O estado e as capacidades são contexto factual; a mensagem atual continua sendo a
  autoridade semântica.
- Use product_action=get_product_link somente quando o cliente pedir o link oficial do
  produto referenciado. Link de produto é diferente de link do carrinho.
- Use checkout_channel_preference=whatsapp ou site quando o cliente escolher
  semanticamente onde deseja continuar.
- Quando FACTS.checkout.requires_channel_choice=true, conduza uma escolha curta entre
  os canais marcados como suportados. Não ofereça um canal com suporte false.
- Se o site for escolhido e site_checkout_supported=true, use somente cart_url.
- Nao repita confirmacao de carrinho quando o estado factual indicar que o item ja esta
  na quantidade desejada. Respeite pending_action e purchase_stage atuais.
- Nunca diga que adicionou, removeu ou alterou quantidade, criou pedido ou confirmou
  pagamento antes de FACTS confirmar sucesso da operacao correspondente.
- Nao pule requisitos factuais do checkout WhatsApp. Se FACTS trouxer bloqueadores,
  continue a conversa obtendo o que falta; a linguagem continua sendo sua decisao.
- cart_url e exclusivamente checkout pelo site. payment_url e exclusivamente o link
  hospedado factual de um pedido ja criado. Nunca use cart_url como Pix, boleto, cartao
  ou fallback de payment_url.
- A ausencia de payment_url nao significa que o metodo selecionado esteja indisponivel.
- Se WhatsApp for escolhido, avance apenas até as capacidades explicitamente marcadas
  como suportadas. Não prometa conclusão de pagamento no chat sem suporte backend.
- Nunca solicite número completo de cartão, CVV, senha, validade ou código de
  autenticação. Use apenas mecanismo seguro/tokenizado quando os FACTS o fornecerem.
- purchase_action=create_cart representa a capacidade protegida de adicionar item;
  inspect_cart consulta o carrinho; show_cart_link obtém seu link; payment_action
  consulta opções/parcelas. IDs e sessões são sempre resolvidos e validados pelo servidor.
""".strip()

SALES_INTERPRETER_INSTRUCTIONS = (
    f"{SALES_INTERPRETER_INSTRUCTIONS}\n\n{CHECKOUT_FLOW_INSTRUCTIONS}"
)
SALES_RESPONDER_INSTRUCTIONS = (
    f"{SALES_RESPONDER_INSTRUCTIONS}\n\n{CHECKOUT_FLOW_INSTRUCTIONS}"
)

_ACTION_TO_PLAN = {
    "product_search": "product_search",
    "product_price": "price",
    "product_inventory": "inventory",
    "coupon_search": "coupon",
}


def _is_greeting(text: str | None) -> bool:
    normalized = " ".join((text or "").lower().strip().split()).strip("!?.,")
    return normalized in {"oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "oi tudo bem", "olá tudo bem", "ola tudo bem"}


def deterministic_scope(text: str | None) -> dict[str, Any]:
    value = (text or "").strip()
    normalized = value.lower()
    if _is_greeting(value):
        return {"domain": "greeting", "action": "greeting", "_source": "fallback"}
    if detect_balance_inquiry(value) or detect_coupon_code_inquiry(value) or detect_raffle_history_inquiry(value) or detect_current_raffle_inquiry(value) or detect_rules_inquiry(value) or "sorteio" in normalized:
        return {"domain": "raffle", "action": "local_flow", "_source": "fallback"}
    if detect_commerce_inquiry(value) or normalized.startswith(("tem ", "vocês têm ", "voces tem ", "vende ")) or any(term in normalized for term in ("comprar", "adquirir", "quero ", "procuro", "busco", "orçamento", "orcamento", "comparar", "recomende")):
        plan = deterministic_sales_plan(value) or {}
        return {"domain": "commerce", **plan, "_source": "fallback"}
    store_terms = ("newstore", "new store", "loja", "pedido", "compra", "atendimento comercial", "catálogo", "catalogo")
    if any(term in normalized for term in store_terms):
        return {"domain": "store_general", "action": "store_general", "_source": "fallback"}
    return {"domain": "out_of_scope", "action": "scope_refusal", "_source": "fallback"}


def _normalize_semantic_plan(parsed: dict[str, Any]) -> dict[str, Any] | None:
    domain = parsed.get("domain")
    if domain not in {"commerce", "raffle", "greeting", "store_general", "out_of_scope"}:
        return None
    normalized: dict[str, Any] = {"domain": domain, "action": parsed.get("action"), "_source": "openai"}
    if domain != "commerce":
        return normalized
    action = parsed.get("action")
    goal = parsed.get("goal")
    if not action and goal:
        action = {"find": "product_search", "recommend": "recommendation", "compare": "product_comparison", "inspect": "product_price", "buy": "purchase_intent", "discover": "clarification"}.get(goal)
    allowed = {"purchase_intent", "product_search", "recommendation", "product_price", "product_inventory", "product_comparison", "coupon_search", "clarification"}
    if action not in allowed:
        return None
    subject = parsed.get("subject") if isinstance(parsed.get("subject"), dict) else {}
    constraints_input = parsed.get("constraints") if isinstance(parsed.get("constraints"), dict) else {}
    query = subject.get("query") or parsed.get("product_query") or subject.get("product_type") or parsed.get("product_type") or subject.get("model") or parsed.get("model") or subject.get("reference") or parsed.get("reference") or subject.get("ean") or parsed.get("ean") or ""
    filters: dict[str, Any] = {}
    for key in ("brand", "model", "reference", "ean", "budget_min", "budget_max", "attributes"):
        value = subject.get(key) if key in {"brand", "model", "reference", "ean"} else constraints_input.get(key, parsed.get(key))
        if value is not None:
            filters[key] = value
    attributes = constraints_input.get("attributes", parsed.get("attributes"))
    if isinstance(attributes, list) and attributes:
        query = " ".join([str(query), *[str(item) for item in attributes]]).strip()
    normalized.update({
        "intent": action,
        "goal": goal or {"purchase_intent": "buy", "product_search": "find", "recommendation": "recommend", "product_comparison": "compare", "product_price": "inspect", "product_inventory": "inspect", "coupon_search": "inspect", "clarification": "discover"}.get(action),
        "subject": {"product_type": subject.get("product_type") or parsed.get("product_type"), "query": str(query).strip(), "brand": filters.get("brand"), "model": filters.get("model"), "reference": filters.get("reference"), "ean": filters.get("ean")},
        "constraints": {
            "budget_min": filters.get("budget_min"),
            "budget_max": filters.get("budget_max"),
            "attributes": filters.get("attributes") or [],
            "color": constraints_input.get("color"),
            "style": constraints_input.get("style"),
            "material": constraints_input.get("material"),
            "explicit_no_preferences": constraints_input.get("explicit_no_preferences") or [],
        },
        "information_needed": parsed.get("information_needed") or ["catalog"],
        "needs_clarification": bool(parsed.get("needs_clarification")),
        "clarification_question": parsed.get("clarification_question"),
        "query": str(query).strip(),
        "filters": filters,
        "budget_max": parsed.get("budget_max"),
        "product_type": parsed.get("product_type"),
    })
    return normalized


def _parse_scope(content: str | None) -> dict[str, Any] | None:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return _normalize_semantic_plan(parsed) if isinstance(parsed, dict) else None


def _fallback_interpretation(text: str | None) -> SalesInterpretation:
    legacy = deterministic_scope(text)
    subject = legacy.get("subject") if isinstance(legacy.get("subject"), dict) else {}
    constraints = legacy.get("constraints") if isinstance(legacy.get("constraints"), dict) else {}
    filters = legacy.get("filters") if isinstance(legacy.get("filters"), dict) else {}
    fallback_goal = legacy.get("goal") or {
        "purchase_intent": "buy",
        "product_search": "find",
        "price": "inspect",
        "inventory": "inspect",
        "coupon": "inspect",
        "recommendation": "recommend",
        "product_comparison": "compare",
        "clarification": "discover",
    }.get(legacy.get("intent"))
    interpretation = SalesInterpretation(
        domain=legacy.get("domain", "out_of_scope"),
        goal=fallback_goal,
        subject={
            "product_type": subject.get("product_type") or legacy.get("product_type"),
            "brand": subject.get("brand") or filters.get("brand"),
            "model": subject.get("model") or filters.get("model"),
            "reference": subject.get("reference") or filters.get("reference"),
            "ean": subject.get("ean") or filters.get("ean"),
        },
        preferences={
            "budget_min": constraints.get("budget_min") or filters.get("budget_min"),
            "budget_max": constraints.get("budget_max") or filters.get("budget_max"),
            "color": constraints.get("color") or filters.get("color"),
            "style": constraints.get("style") or filters.get("style"),
            "material": constraints.get("material") or filters.get("material"),
            "attributes": constraints.get("attributes") or filters.get("attributes") or [],
            "explicit_no_preferences": constraints.get("explicit_no_preferences") or [],
        },
        information_needed=["catalog"] if legacy.get("domain") == "commerce" else [],
        references_previous_context=False,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        stop_clarification=False,
        needs_clarification=bool(legacy.get("needs_clarification")),
        clarification_question=legacy.get("clarification_question"),
        confidence=0.6,
    )
    interpretation._source = "deterministic_fallback"
    from .preference_normalize import normalize_sales_interpretation
    from .turn_understanding import sales_to_turn_understanding

    normalized = normalize_sales_interpretation(interpretation, message_text=text)
    normalized._turn_understanding = sales_to_turn_understanding(
        normalized, message_text=text
    )
    return normalized


def _rehydrate_contact_preferences(
    interpretation: SalesInterpretation,
    message: IncomingMessage,
) -> SalesInterpretation:
    """Seed empty preference fields from durable contact memory."""
    try:
        from .contact_preference_memory import (
            rehydrate_interpretation_from_contact_memory,
        )

        settings = get_settings()
        sender_key = message.sender_key or (
            f"whatsapp:{message.sender_phone}" if message.sender_phone else None
        )
        return rehydrate_interpretation_from_contact_memory(
            interpretation,
            tenant_id=str(getattr(settings, "agent_persona_tenant_id", "newstore")),
            sender_key=sender_key,
        )
    except Exception as exc:
        print(
            "[memory.contact_preference.rehydrate_hook_error]",
            {"error_type": type(exc).__name__, "error": str(exc)[:120]},
        )
        return interpretation


def _log_interpretation(
    interpretation: SalesInterpretation,
    model: str,
    *,
    fallback_reason: str | None = None,
) -> None:
    preferences = interpretation.preferences
    turn = getattr(interpretation, "_turn_understanding", None)
    payload = {
        "source": interpretation._source,
        "model": model,
        "domain": interpretation.domain,
        "goal": interpretation.goal,
        "confidence": interpretation.confidence,
        "references_previous_context": interpretation.references_previous_context,
        "has_product_type": bool(interpretation.subject.product_type),
        "has_brand": bool(interpretation.subject.brand),
        "has_style": bool(preferences.style),
        "has_color": bool(preferences.color),
        "has_budget": preferences.budget_min is not None or preferences.budget_max is not None,
        "enough_information_to_search": interpretation.enough_information_to_search,
        "ready_for_retrieval": interpretation.ready_for_retrieval,
        "stop_clarification": interpretation.stop_clarification,
        "needs_clarification": interpretation.needs_clarification,
    }
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    if turn is not None:
        payload.update(
            {
                "primary_intent": getattr(turn, "primary_intent", None),
                "answer_strategy": getattr(turn, "answer_strategy", None),
                "clarification_required": getattr(turn, "clarification_required", None),
                "hard_brand": bool(getattr(getattr(turn, "hard_constraints", None), "brand", None)),
                "hard_budget_max": getattr(
                    getattr(turn, "hard_constraints", None), "budget_max", None
                ),
                "ambiguity_count": len(getattr(turn, "ambiguity", None) or []),
                "blocking_ambiguity": sum(
                    1
                    for item in (getattr(turn, "ambiguity", None) or [])
                    if getattr(item, "blocking", False)
                ),
            }
        )
    print("[sales.interpreter]", payload)


def _normalize_interpreter_history(
    recent_turns: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def _sanitize_openai_error_message(value: object) -> str:
    message = str(value or "OpenAI rejected the interpreter request")
    message = re.sub(r"sk-(?:proj-)?[A-Za-z0-9_-]+", "sk-***", message)
    message = re.sub(r"(?i)(authorization\s*[:=]?\s*bearer)\s+\S+", r"\1 ***", message)
    return message[:600]


def _bad_request_details(exc: BadRequestError, model: str) -> dict[str, Any]:
    body = exc.body if isinstance(exc.body, dict) else {}
    body_error = body.get("error") if isinstance(body.get("error"), dict) else body
    code = getattr(exc, "code", None) or body_error.get("code")
    param = getattr(exc, "param", None) or body_error.get("param")
    message = getattr(exc, "message", None) or body_error.get("message") or str(exc)
    return {
        "error_type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None),
        "error_code": code,
        "error_param": param,
        "error_message": _sanitize_openai_error_message(message),
        "model": model,
    }


def interpretation_to_plan(
    interpretation: SalesInterpretation,
    text: str | None = None,
) -> dict[str, Any]:
    subject = interpretation.subject.model_dump()
    preferences = interpretation.preferences.model_dump()
    if subject.get("reference"):
        query_parts = [str(subject["reference"])]
    elif subject.get("ean"):
        query_parts = [str(subject["ean"])]
    elif subject.get("brand") or subject.get("model"):
        query_parts = [str(value) for value in (subject.get("brand"), subject.get("model")) if value]
    elif subject.get("product_type"):
        query_parts = [str(subject["product_type"])]
    else:
        query_parts = []
    query = " ".join(query_parts).strip()

    information_needed = set(interpretation.information_needed)
    inspect_intent = (
        "inventory" if "inventory" in information_needed
        else "coupon" if "coupons" in information_needed
        else "price" if information_needed.intersection({"price", "payment"})
        else "product_search"
    )
    goal_to_intent = {
        "discover": "clarification",
        "find": "product_search",
        "recommend": "recommendation",
        "compare": "product_comparison",
        "inspect": inspect_intent,
        "buy": "clarification",
        "after_sales": "clarification",
    }
    retrieval_signal = any((
        interpretation.enough_information_to_search,
        interpretation.ready_for_retrieval,
        interpretation.stop_clarification,
    ))
    if interpretation.purchase_action == "create_cart":
        intent = "purchase_intent"
    elif retrieval_signal and interpretation.goal in {"discover", "recommend", "buy"}:
        intent = "recommendation"
    else:
        intent = "clarification" if interpretation.needs_clarification else goal_to_intent.get(
            interpretation.goal or "discover",
            "clarification",
        )
    filters = {
        key: value
        for key, value in {
            "brand": subject.get("brand"),
            "model": subject.get("model"),
            "reference": subject.get("reference"),
            "ean": subject.get("ean"),
            "budget_min": preferences.get("budget_min"),
            "budget_max": preferences.get("budget_max"),
            "attributes": preferences.get("attributes"),
            "color": preferences.get("color"),
            "style": preferences.get("style"),
            "material": preferences.get("material"),
        }.items()
        if value not in (None, [], "")
    }
    return {
        "domain": interpretation.domain,
        "intent": intent,
        "goal": interpretation.goal,
        "subject": {**subject, "query": query},
        "constraints": preferences,
        "query": query,
        "filters": filters,
        "budget_max": preferences.get("budget_max"),
        "product_type": subject.get("product_type"),
        "needs_clarification": interpretation.needs_clarification,
        "clarification_question": interpretation.clarification_question,
        "information_needed": interpretation.information_needed,
        "enough_information_to_search": interpretation.enough_information_to_search,
        "ready_for_retrieval": interpretation.ready_for_retrieval,
        "stop_clarification": interpretation.stop_clarification,
        "purchase_action": interpretation.purchase_action,
        "quantity": interpretation.quantity,
        "purchase_items": [
            item.model_dump(mode="json")
            for item in interpretation.purchase_items
        ],
        "image_request": interpretation.image_request,
        "product_action": interpretation.product_action,
        "payment_action": interpretation.payment_action,
        "payment_method_preference": interpretation.payment_method_preference,
        "payment_option_id": interpretation.payment_option_id,
        "checkout_channel_preference": interpretation.checkout_channel_preference,
        "shipping_action": interpretation.shipping_action,
        "shipping_zipcode": interpretation.shipping_zipcode,
        "shipping_selection_id": interpretation.shipping_selection_id,
        "shipping_selection_position": interpretation.shipping_selection_position,
        "checkout_action": interpretation.checkout_action,
        "checkout_data": (
            interpretation.checkout_data.model_dump(mode="json", exclude_none=True)
            if interpretation.checkout_data else None
        ),
        "order_action": interpretation.order_action,
        "order_id": interpretation.order_id,
        "confirmation": interpretation.confirmation,
        "installment_count": interpretation.installment_count,
        "_source": interpretation._source,
    }


async def interpret_message(
    message: IncomingMessage,
    *,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> SalesInterpretation:
    settings = get_settings()
    if _is_greeting(message.text):
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "greeting_fast_path"
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="greeting_fast_path")
        return fallback
    if not settings.openai_api_key:
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "openai_api_key_missing"
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="openai_api_key_missing")
        return fallback
    current_text = (message.text or "").strip()
    if not current_text:
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "empty_message"
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="empty_message")
        return fallback

    from .capability_catalog import format_capability_catalog_for_prompt
    from .turn_understanding import (
        TURN_UNDERSTANDING_INSTRUCTIONS,
        TurnUnderstanding,
        apply_clarification_policy,
        sanitize_turn_understanding,
        sales_to_turn_understanding,
        turn_understanding_to_sales,
    )

    from .rollout import is_turn_understanding_enabled

    use_turn_understanding = is_turn_understanding_enabled(settings)
    if use_turn_understanding:
        interpreter_model = (
            (getattr(settings, "openai_fast_model", None) or "").strip()
            or (getattr(settings, "openai_main_model", None) or "").strip()
            or settings.openai_model
        )
    else:
        interpreter_model = settings.openai_model

    normalized_history = _normalize_interpreter_history(recent_turns)
    state_obj = commerce_state or CommerceConversationState()
    state_message = {
        "role": "system",
        "content": (
            "COMMERCE_STATE:\n"
            + json.dumps(state_obj.interpreter_payload(), ensure_ascii=False)
            + "\n\nWORKING_MEMORY:\n"
            + json.dumps(build_working_memory(state_obj), ensure_ascii=False)
            + "\n"
            + WORKING_MEMORY_USAGE_POLICY
            + "\n\n"
            + format_capability_catalog_for_prompt()
        ),
    }
    system_instructions = (
        TURN_UNDERSTANDING_INSTRUCTIONS
        if use_turn_understanding
        else SALES_INTERPRETER_INSTRUCTIONS
    )
    try:
        from .persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        if runtime is not None and runtime.enabled:
            system_instructions = (
                f"{system_instructions}\n\n{runtime.interpreter_policy_block()}"
            )
    except Exception:
        pass
    messages = [
        {"role": "system", "content": system_instructions},
        state_message,
        *normalized_history,
        {"role": "user", "content": current_text},
    ]
    print("[sales.interpreter.request]", {
        "model": interpreter_model,
        "structured_output": True,
        "turn_understanding": use_turn_understanding,
        "history_turns": len(normalized_history),
        "message_count": len(messages),
        "has_temperature": True,
        "has_max_tokens": False,
        "has_tools": False,
    })
    try:
        from .openai_errors import OpenAIGatewayError, OpenAIRefusalError
        from .openai_gateway import parse_structured_output
        from .preference_normalize import (
            normalize_sales_interpretation,
            recent_user_context_text,
        )

        text_format = TurnUnderstanding if use_turn_understanding else SalesInterpretation
        parse_result = await parse_structured_output(
            model=interpreter_model,
            text_format=text_format,
            messages=messages,
            temperature=0,
            call_type="decision",
        )
        parsed = parse_result.parsed
        if use_turn_understanding:
            if isinstance(parsed, TurnUnderstanding):
                understanding = sanitize_turn_understanding(parsed)
                has_recoverable_reference = bool(
                    getattr(state_obj, "last_presented_products", None)
                    or getattr(state_obj, "active_product", None)
                )
                understanding = apply_clarification_policy(
                    understanding,
                    message_text=current_text,
                    has_recoverable_reference=has_recoverable_reference,
                )
                understanding._source = "openai"
                interpretation = turn_understanding_to_sales(understanding)
            elif isinstance(parsed, SalesInterpretation):
                # Compatibility: legacy schema / test fakes during rollout.
                interpretation = parsed
                interpretation._source = "openai"
                interpretation._turn_understanding = sales_to_turn_understanding(
                    interpretation, message_text=current_text
                )
            else:
                raise ValueError("interpreter_turn_understanding_missing")
        else:
            if not isinstance(parsed, SalesInterpretation):
                raise ValueError("interpreter_schema_missing")
            interpretation = parsed
            interpretation._source = "openai"
            # Shadow: keep a TurnUnderstanding view for metrics / later cutover.
            interpretation._turn_understanding = sales_to_turn_understanding(
                interpretation, message_text=current_text
            )

        interpretation = normalize_sales_interpretation(
            interpretation,
            message_text=current_text,
            context_text=recent_user_context_text(recent_turns),
        )
        interpretation = _rehydrate_contact_preferences(interpretation, message)
        # Re-sync TurnUnderstanding after preference normalization when present.
        if interpretation._turn_understanding is None:
            interpretation._turn_understanding = sales_to_turn_understanding(
                interpretation, message_text=current_text
            )
        _log_interpretation(interpretation, interpreter_model)
        return interpretation
    except BadRequestError as exc:
        print("[sales.interpreter.error]", _bad_request_details(exc, interpreter_model))
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "openai_bad_request"
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(fallback, interpreter_model, fallback_reason="openai_bad_request")
        return fallback
    except OpenAIRefusalError as exc:
        print("[sales.interpreter] failed", {"error_type": type(exc).__name__})
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "openai_invalid_response"
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(
            fallback,
            interpreter_model,
            fallback_reason="openai_invalid_response",
        )
        return fallback
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        ValidationError,
        ValueError,
        TypeError,
    ) as exc:
        print("[sales.interpreter] failed", {"error_type": type(exc).__name__})
        fallback = _fallback_interpretation(message.text)
        fallback_reason = "openai_request_failed" if isinstance(exc, APIError) else "openai_invalid_response"
        fallback._fallback_reason = fallback_reason
        fallback = _rehydrate_contact_preferences(fallback, message)
        _log_interpretation(fallback, interpreter_model, fallback_reason=fallback_reason)
        return fallback


def deterministic_sales_plan(text: str | None) -> dict[str, Any] | None:
    normalized = (text or "").lower()
    purchase = any(term in normalized for term in ("quero comprar", "quero adquirir", "quero um ", "quero uma ", "gostaria de comprar", "gostaria de um ", "procuro", "busco", "recomende"))
    action = resolve_commerce_action(text)
    if purchase and not any(term in normalized for term in ("quanto custa", "preço", "preco", "estoque", "disponibilidade")):
        action = "purchase_intent"
    if not action:
        return None
    query = extract_product_query(text)
    budget_max = None
    budget_match = re.search(r"(?:até|ate|por|no máximo|até o limite de)\s*(?:r\$\s*)?([\d.,]+)\s*(mil|k)?", query, flags=re.IGNORECASE)
    if budget_match:
        raw = budget_match.group(1).replace(".", "").replace(",", ".")
        budget_max = float(raw) * (1000 if budget_match.group(2) else 1)
        query = (query[:budget_match.start()] + query[budget_match.end():]).strip(" ,-")
    if query.lower().strip() in {"alguma coisa", "algo", "qualquer coisa", "um produto", "uma coisa"}:
        query = ""
    ean_match = re.fullmatch(r"(?:ean\s+)?(\d{8,14})", query, flags=re.IGNORECASE)
    reference = None
    if not ean_match and query and (
        re.search(r"[./_-]", query)
        or (re.search(r"\d", query) and re.search(r"[A-Za-z]", query) and " " not in query)
    ):
        reference = re.sub(r"^(?:sku|ref(?:er[êe]ncia)?)\s+", "", query, flags=re.IGNORECASE)
    fallback_product_type = None
    fallback_model = None
    if query and not ean_match and not reference:
        if action == "product_search":
            fallback_model = query
        else:
            fallback_product_type = query.split()[0] if action == "purchase_intent" else query
    from .sales.discovery import _mentioned_watch_brands

    brands = _mentioned_watch_brands(text)
    brand = brands[0] if brands else None
    model = fallback_model
    if brand and model:
        leftover = model.casefold()
        for hit in brands:
            leftover = leftover.replace(hit.casefold(), " ")
        for token in ("relógio", "relogio", "watch"):
            leftover = leftover.replace(token, " ")
        leftover = " ".join(leftover.split())
        model = leftover or None
        if any(token in (fallback_model or "").casefold() for token in ("relógio", "relogio")):
            fallback_product_type = fallback_product_type or "relógio"
    plan: dict[str, Any] = {
        "intent": "purchase_intent" if action == "purchase_intent" else _ACTION_TO_PLAN.get(action, "product_search"),
        "query": query,
        "filters": {"budget_max": budget_max} if budget_max is not None else {},
        "goal": "recommend" if budget_max is not None or (len(query.split()) > 1 and action == "purchase_intent") else ("buy" if action == "purchase_intent" else None),
        "subject": {
            "product_type": fallback_product_type,
            "query": query,
            "ean": ean_match.group(1) if ean_match else None,
            "reference": reference,
        },
        "constraints": {"budget_max": budget_max, "attributes": query.split()[1:] if budget_max is not None and len(query.split()) > 1 else []},
    }
    plan["subject"].update({"brand": brand, "model": model})
    if brand and "brand" not in plan["filters"]:
        plan["filters"] = {**plan["filters"], "brand": brand}
    return plan


def _parse_plan(content: str | None) -> dict[str, Any] | None:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    allowed_intents = {"product_search", "price", "inventory", "coupon", "recommendation"}
    intent = parsed.get("intent")
    if intent not in allowed_intents:
        return None
    query = parsed.get("query")
    if query is not None and not isinstance(query, str):
        return None
    filters = parsed.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    return {
        "intent": intent,
        "query": (query or "").strip(),
        "filters": {key: value for key, value in filters.items() if key in {"brand", "category_id", "budget_max", "style", "color"}},
        "budget_max": parsed.get("budget_max"),
    }


async def plan_sales_request(message: IncomingMessage) -> dict[str, Any] | None:
    interpretation = await interpret_message(message)
    if interpretation.domain != "commerce":
        return None
    return interpretation_to_plan(interpretation, message.text)


from .sales.workflows.catalog_ranking import (  # noqa: E402
    candidate_price as _candidate_price,
    candidate_text as _candidate_text,
    fold_text as _fold,
    rank_candidates,
    score_candidate,
)


def _ranked_result(result: AgentResult, plan: dict[str, Any]) -> AgentResult | None:
    data = result.commercial_data or {}
    products = data.get("products") if isinstance(data.get("products"), list) else []
    selected = rank_candidates(products, plan)
    if not selected:
        return None
    from .commerce_router import _product_result

    action = "product_price" if plan.get("intent") == "price" else "product_search"
    ranked = _product_result(action, selected)
    if data.get("inventory") is not None:
        inventory = data["inventory"]
        ranked.reply_text = "Consulta de estoque:\n" + "\n".join(_product_lines(selected, inventory))
        ranked.commercial_data = {"products": selected, "inventory": inventory}
    return ranked


from .sales.result_utils import mark_sales_result as _mark_sales_result


from .sales.discovery import (  # noqa: E402
    _is_clarification_turn,
    _consecutive_clarification_count,
    _known_preferences,
    _specific_product_lock,
    _subject_identifiable,
    _persona_requires_qualification,
    _preference_key_set,
    _has_urgency_signal,
    build_qualification_snapshot,
    _persona_qualification_question,
    _needs_persona_qualification,
    _comparison_needs_qualification,
    _mentioned_watch_brands,
    _comparison_clarification_question,
    _discovery_state,
    _needs_clarification_before_retrieval,
    is_open_catalog_browse_request,
)


async def generate_clarification_reply(
    *,
    message: IncomingMessage,
    interpretation: SalesInterpretation,
    recent_turns: list[dict[str, Any]] | None = None,
    context_note: str | None = None,
    used_tray: bool = False,
    discovery_state: dict[str, Any] | None = None,
) -> AgentResult:
    settings = get_settings()
    persona_gate = bool(
        isinstance(discovery_state, dict)
        and discovery_state.get("persona_qualification_required")
    )
    persona_question = None
    if persona_gate:
        persona_question = _persona_qualification_question(interpretation, discovery_state)
        if persona_question:
            interpretation = interpretation.model_copy(
                update={
                    "needs_clarification": True,
                    "clarification_question": persona_question,
                    "ready_for_retrieval": False,
                }
            )

    # Prefer persona-sourced question as the containment hint; never invent copy.
    deterministic_question = persona_question or interpretation.clarification_question
    if not deterministic_question or _comparison_needs_qualification(interpretation):
        if _comparison_needs_qualification(interpretation):
            deterministic_question = _comparison_clarification_question(
                message, interpretation
            )
        else:
            deterministic_question = (
                interpretation.clarification_question
                or _persona_qualification_question(interpretation, discovery_state)
            )

    # Interpreter-authored question can be customer-facing only when the persona
    # gate is NOT forcing qualification (otherwise rephrase with persona).
    if (
        interpretation._source == "openai"
        and interpretation.clarification_question
        and not _comparison_needs_qualification(interpretation)
        and not persona_gate
    ):
        return _mark_sales_result(
            AgentResult(
                reply_text=html.unescape(interpretation.clarification_question.strip()),
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            interpretation=interpretation,
            goal=interpretation.goal,
            response_source="openai",
            used_openai_responder=False,
            used_tray=used_tray,
        )
    if not settings.openai_api_key:
        reply = (deterministic_question or "").strip()
        return _mark_sales_result(
            AgentResult(
                reply_text=reply or (persona_question or ""),
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            interpretation=interpretation,
            goal=interpretation.goal,
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=used_tray,
            fallback_reason="openai_api_key_missing",
        )

    normalized_history = _normalize_interpreter_history(recent_turns)
    persona_runtime = None
    try:
        from .persona_runtime import get_persona_runtime

        persona_runtime = get_persona_runtime()
    except Exception:
        persona_runtime = None
    persona_blocks: list[str] = [SALES_CLARIFICATION_INSTRUCTIONS]
    if persona_runtime and persona_runtime.enabled:
        policy = (persona_runtime.prompt_policy_block() or "").strip()
        skills = (persona_runtime.sales_skills_block(max_items=8) or "").strip()
        if policy:
            persona_blocks.append(policy)
        if skills:
            persona_blocks.append(skills)
    request_context = {
        "current_message": message.text,
        "interpretation": interpretation.model_dump(),
        "context_note": context_note,
        "persona_qualification_hint": persona_question,
        "DISCOVERY_STATE": discovery_state
        or _discovery_state(
            interpretation,
            recent_turns,
            message_text=message.text,
        ),
    }
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import generate_text_output

        clarification_messages = [
            {"role": "system", "content": "\n\n".join(persona_blocks)},
            *normalized_history,
            {"role": "user", "content": json.dumps(request_context, ensure_ascii=False)},
        ]
        text_result = await generate_text_output(
            model=settings.openai_model,
            messages=clarification_messages,
            temperature=0.3,
            call_type="clarification",
        )
        content = text_result.text
        if not content or not content.strip():
            raise ValueError("clarification_response_empty")
        return _mark_sales_result(
            AgentResult(
                reply_text=html.unescape(content.strip()),
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            interpretation=interpretation,
            goal=interpretation.goal,
            response_source="openai",
            used_openai_responder=True,
            used_tray=used_tray,
        )
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError) as exc:
        print("[sales.clarification] failed", {"error_type": type(exc).__name__})
        fallback = (deterministic_question or persona_question or "").strip()
        return _mark_sales_result(
            AgentResult(
                reply_text=fallback,
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            interpretation=interpretation,
            goal=interpretation.goal,
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=used_tray,
            fallback_reason="clarification_responder_failed",
        )


async def _sales_response_with_openai(
    message: IncomingMessage,
    plan: dict[str, Any],
    tray_result: AgentResult,
    interpretation: SalesInterpretation | None = None,
    state: CommerceConversationState | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
) -> AgentResult | None:
    from .capability_catalog import (
        build_capability_catalog,
        format_capability_catalog_for_prompt,
    )

    settings = get_settings()
    if not settings.openai_api_key or tray_result.safety_reason in {
        "tray_adapter_unavailable", "product_match_failed", "product_not_found",
        "ambiguous_product", "product_context_missing", "coupon_not_found",
        "order_not_found", "order_status_technical_failure",
        "invalid_customer_document", "order_customer_not_confirmed",
        "order_customer_mismatch", "order_customer_lookup_technical_failure",
        "customer_orders_lookup_technical_failure",
        # Keep deterministic storefront-link fallbacks (don't invent João handoff).
        "product_media_link_fallback",
        "product_image_link_fallback",
    }:
        return None
    if (tray_result.commercial_data or {}).get("input_template"):
        return None
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import generate_text_output

        from .prompt_compiler import (
            legacy_contract_extra_blocks,
            resolve_system_instructions,
        )

        responder_contract = (
            f"{SALES_RESPONDER_INSTRUCTIONS}\n\n"
            f"{channel_system_hint(message.channel)}\n\n"
            f"{format_capability_catalog_for_prompt()}"
        )
        history_turns = (
            recent_turns
            if recent_turns is not None
            else _sales_recent_turns.get()
        )
        history = _normalize_interpreter_history(history_turns)
        responder_instructions = resolve_system_instructions(
            fallback_instructions=responder_contract,
            incoming=message,
            conversation_state=state,
            recent_turns=history_turns,
            extra_system_blocks=legacy_contract_extra_blocks(
                responder_contract,
                tag="sales_responder_contract",
            ),
        )
        memory_sidechannel = bool(
            getattr(settings, "agent_memory_proposals_enabled", False)
            or getattr(settings, "agent_instruction_extension_proposals_enabled", False)
            or getattr(settings, "agent_conversation_summary_enabled", False)
        )
        if memory_sidechannel:
            from .memory_policy import MEMORY_POLICY_PROMPT

            responder_instructions = (
                f"{responder_instructions}\n\n{MEMORY_POLICY_PROMPT}"
            )

        from .history_window import resolve_model_history_limit

        model_history_limit = resolve_model_history_limit(settings)
        responder_messages = [
            {"role": "system", "content": responder_instructions},
            *history[-model_history_limit:],
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "original_message": message.text,
                        "plan": plan,
                        "STATE_FACTS": (
                            state.interpreter_payload() if state else {}
                        ),
                        "WORKING_MEMORY": (
                            build_working_memory(state) if state else {}
                        ),
                        "AVAILABLE_CAPABILITIES": build_capability_catalog(),
                        "RESPONSE_CONTRACT": _responder_contract(state),
                        "FACTS": tray_result.commercial_data or {"summary": tray_result.reply_text},
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        envelope = None
        if memory_sidechannel:
            from .memory_models import AgentTurnEnvelope
            from .openai_gateway import parse_structured_output

            try:
                parse_result = await parse_structured_output(
                    model=settings.openai_model,
                    text_format=AgentTurnEnvelope,
                    messages=responder_messages,
                    temperature=0.3,
                    call_type="response_composition_envelope",
                )
                envelope = parse_result.parsed
                content = getattr(envelope, "reply", None) if envelope is not None else None
            except (APIError, OpenAIGatewayError, BadRequestError, ValueError, TypeError):
                print("[sales.responder] envelope_failed_fallback_text")
                text_result = await generate_text_output(
                    model=settings.openai_model,
                    messages=responder_messages,
                    temperature=0.3,
                    call_type="response_composition",
                )
                content = text_result.text
        else:
            text_result = await generate_text_output(
                model=settings.openai_model,
                messages=responder_messages,
                temperature=0.3,
                call_type="response_composition",
            )
            content = text_result.text
        if not content or not content.strip():
            return None
        final_result = AgentResult(
            reply_text=html.unescape(content.strip()),
            intent="commerce",
            handoff_required=False,
            safety_reason=tray_result.safety_reason,
            commercial_data=tray_result.commercial_data,
            response_metadata=dict(tray_result.response_metadata),
        )
        final_result.response_metadata.setdefault(
            "factual_fallback_text",
            tray_result.reply_text,
        )
        if envelope is not None:
            final_result.response_metadata["agent_turn_envelope"] = envelope.model_dump(
                mode="json"
            )
        return _mark_sales_result(
            final_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="openai",
            used_openai_responder=True,
            used_tray=bool(tray_result.response_metadata.get("used_tray", True)),
        )
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError) as exc:
        print("[sales.responder] failed", {"error_type": type(exc).__name__})
        return None


def _responder_contract(state: CommerceConversationState | None) -> dict[str, Any]:
    """Factual response constraints, independent of the wording generated by GPT."""
    if state is None:
        return {"product_state": "unknown", "order_state": "not_created"}
    revalidation = state.order_payment_revalidation_status
    if revalidation == "unavailable":
        payment_method_state = "unavailable"
    elif state.selected_payment_option is not None:
        payment_method_state = "available"
    elif state.payment_method_preference:
        payment_method_state = "pending_selection"
    else:
        payment_method_state = "not_selected"
    checkout = checkout_capabilities(state)
    confirmation_required = bool(
        state.pending_action == "awaiting_order_confirmation"
        and state.order_confirmation_status == "pending"
        and state.order_review_version
    )
    return {
        "product_state": "in_cart" if state.cart_items else "missing",
        "payment_method_state": payment_method_state,
        "payment_link_state": (
            "available" if state.order_payment_url
            else "pending" if state.order_id else "not_created"
        ),
        "order_state": "created" if state.order_id else "not_created",
        "order_payment_revalidation_status": revalidation,
        "hosted_payment_supported": bool(
            checkout.get("whatsapp_hosted_payment_supported")
        ),
        "customer_confirmation_required": confirmation_required,
        "payment_payable_total": None,
    }


from .sales.policies.confirmation import (  # noqa: E402
    confirmation_text_kind as _confirmation_text_kind,
)


from .sales.product_lookup import (  # noqa: E402
    execute_compiled_product_retrieval as _execute_compiled_product_retrieval,
    execute_contextual_product_lookup as _execute_contextual_product_lookup,
)
from .sales.checkout_flow import (  # noqa: E402
    _advance_whatsapp_checkout,
    _combine_cart_and_payment_results,
    _combine_checkout_and_followup_results,
    _combine_checkout_channel_result,
    _combine_order_and_payment_results,
    _confirm_current_order_review,
    _create_order_with_payment_lookup,
    _ensure_cart_for_purchase,
    _fulfill_confirmed_order,
    _inspect_listed_products,
    _order_payment_revalidation,
    _pending_action_rejected_result,
    _pending_product_references,
    respond_to_commerce_service as _respond_to_commerce_service,
)


from .sales.policies.action_authority import (  # noqa: E402
    informational_payment_policy_result as _informational_payment_policy_result,
    is_informational_payment_query as _is_informational_payment_query,
    purchase_product_required_result as _purchase_product_required_result,
)



async def handle_sales_message(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    semantic_plan: dict[str, Any] | SalesInterpretation | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> AgentResult | None:
    history_token = _sales_recent_turns.set(recent_turns)
    try:
        return await _handle_sales_message_inner(
            message,
            facts,
            customer_context,
            semantic_plan=semantic_plan,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
        )
    finally:
        _sales_recent_turns.reset(history_token)


async def _handle_sales_message_inner(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    semantic_plan: dict[str, Any] | SalesInterpretation | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> AgentResult | None:
    if message.input_modality == "audio" and (
        message.transcription_failed or not (message.text or "").strip()
    ):
        return AgentResult(
            reply_text=(
                "Recebi seu áudio, mas não consegui transcrever. "
                "Pode repetir por texto ou enviar outro áudio?"
            ),
            intent="audio_transcription_failed",
            handoff_required=False,
        )

    interpretation = semantic_plan if isinstance(semantic_plan, SalesInterpretation) else None
    if interpretation is not None:
        from .preference_normalize import (
            normalize_sales_interpretation,
            recent_user_context_text,
        )

        interpretation = normalize_sales_interpretation(
            interpretation,
            message_text=message.text,
            context_text=recent_user_context_text(recent_turns),
            recent_turns=recent_turns,
        )
        interpretation = _rehydrate_contact_preferences(interpretation, message)
    from .commerce_router import (
        is_listed_catalog_follow_up,
        is_outbound_catalog_image_request,
        wants_all_listed_product_images,
    )

    if interpretation is not None and is_outbound_catalog_image_request(message.text):
        interpretation = interpretation.model_copy(update={"image_request": True})
    state = commerce_state or CommerceConversationState()
    if interpretation is not None:
        from .sales.purchase_selection import repair_presented_purchase_selection

        interpretation = repair_presented_purchase_selection(
            interpretation,
            message_text=message.text,
            state=state,
            recent_turns=recent_turns,
        )
    deterministic_confirmation = _confirmation_text_kind(state, message.text)
    if deterministic_confirmation == "confirm":
        return await _confirm_current_order_review(
            message=message,
            plan={"intent": "commerce", "goal": "buy"},
            state=state,
            source="contextual_text",
        )
    if deterministic_confirmation == "reject":
        rejected = AgentResult(
            reply_text="A confirmação do pedido foi cancelada.",
            intent="commerce",
            response_metadata={
                "domain": "commerce",
                "clear_pending_action": True,
                "order_state": {
                    "order_confirmation_status": "not_ready",
                    "order_review_version": None,
                    "confirmed_order_review_version": None,
                },
                "used_tray": False,
            },
        )
        print("[sales.order.confirmation.turn]", {
            "pending_action_before": state.pending_action,
            "confirmation_source": "contextual_text",
            "explicit_change_detected": False,
            "review_version_present": bool(state.order_review_version),
            "confirmed_review_version_present": bool(state.confirmed_order_review_version),
            "branch_taken": "reject_order_review",
            "prepare_order_called": False,
            "confirm_prepared_order_called": False,
            "create_order_called": False,
            "pending_action_after": None,
        })
        return await _respond_to_commerce_service(
            message=message,
            plan={"intent": "commerce", "goal": "buy"},
            result=rejected,
            interpretation=None,
            state=evolve_commerce_state(state, rejected),
        )

    from .sales.policies.objection_authority import try_objection_authority_result

    objection_result = try_objection_authority_result(message, interpretation, state)
    if objection_result is not None:
        print("[sales.objection.authority]", {
            "kind": (objection_result.response_metadata or {}).get("objection_kind"),
            "handoff": objection_result.handoff_required,
        })
        return _mark_sales_result(
            objection_result,
            interpretation=interpretation,
            goal=(interpretation.goal if interpretation is not None else "discover"),
            response_source="deterministic_objection",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=objection_result.safety_reason,
        )

    log_purchase_progress("interpretation", "start")
    if interpretation is not None:
        plan = interpretation_to_plan(interpretation, message.text)
    elif isinstance(semantic_plan, SalesInterpretation):
        plan = interpretation_to_plan(semantic_plan, message.text)
    elif semantic_plan and semantic_plan.get("domain") == "commerce":
        plan = semantic_plan
    else:
        plan = await plan_sales_request(message)
    if not plan:
        log_purchase_progress(
            "interpretation",
            "blocked",
            "sales_plan_missing",
        )
        return None
    log_purchase_progress("interpretation", "success")
    if (
        interpretation is not None
        and interpretation.active_topic == "purchase_option_choice"
        and interpretation.needs_clarification
        and str(interpretation.clarification_question or "").strip()
    ):
        return _mark_sales_result(
            AgentResult(
                reply_text=html.unescape(
                    str(interpretation.clarification_question).strip()
                ),
                intent="commerce",
                handoff_required=False,
                safety_reason="purchase_option_choice",
                response_metadata={"domain": "commerce"},
            ),
            interpretation=interpretation,
            goal="buy",
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="purchase_option_choice",
        )
    if interpretation is not None:
        print("[sales.semantic.result]", {
            "scope_domain": interpretation.domain,
            "intent": plan.get("intent"),
            "goal": interpretation.goal,
            "reference_type": interpretation.reference_type,
            "has_subject": bool(
                interpretation.subject.product_type
                or interpretation.subject.brand
                or interpretation.subject.model
                or interpretation.subject.reference
                or interpretation.subject.ean
            ),
            "purchase_action": interpretation.purchase_action,
            "product_action": interpretation.product_action,
            "payment_action": interpretation.payment_action,
            "checkout_channel_preference": interpretation.checkout_channel_preference,
            "image_request": interpretation.image_request,
            "confirmation": interpretation.confirmation,
            "pending_action_disposition": (
                interpretation.confirmation
                if state.pending_action
                else "none"
            ),
        })
    print("[sales.purchase.orchestrator]", {
        "has_purchase_action": bool(
            interpretation and interpretation.purchase_action
        ),
        "has_payment_action": bool(
            interpretation and interpretation.payment_action
        ),
        "has_active_product": state.active_product is not None,
        "purchase_item_count": len(
            interpretation.purchase_items
            if interpretation is not None
            else []
        ),
        "reference_type": (
            interpretation.reference_type
            if interpretation is not None
            else None
        ),
        "reference_position_present": bool(
            interpretation
            and interpretation.reference_position is not None
        ),
        "confirmation": (
            interpretation.confirmation
            if interpretation is not None
            else None
        ),
        "has_pending_action": bool(state.pending_action),
        "current_purchase_stage": state.purchase_stage,
    })
    if interpretation is not None and interpretation.payment_action == "order_payment":
        if state.pix_payment_id and not state.order_id:
            payment_result = await refresh_direct_pix_checkout(state=state)
        else:
            payment_result = await inspect_order_payment(
                state=state,
                execute=execute_tool,
                order_id=interpretation.order_id,
            )
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=payment_result,
            interpretation=interpretation,
            state=evolve_commerce_state(state, payment_result),
        )
    if interpretation is not None and interpretation.order_action is not None:
        order_result = await get_order_facts(
            state=state,
            execute=execute_tool,
            order_id=interpretation.order_id,
        )
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=order_result,
            interpretation=interpretation,
            state=evolve_commerce_state(state, order_result),
        )
    if (
        interpretation is not None
        and state.pending_action == "awaiting_order_confirmation"
        and interpretation.confirmation == "confirm"
        and interpretation.purchase_action not in {
            "set_cart_item_quantity", "remove_cart_item",
        }
        and interpretation.checkout_data is None
        and interpretation.shipping_action is None
        and interpretation.payment_action is None
        and interpretation.checkout_channel_preference is None
        and not interpretation.domain_change_explicit
    ):
        confirmed = confirm_prepared_order(state)
        confirmed_state = evolve_commerce_state(state, confirmed)
        order_result = await _fulfill_confirmed_order(
            confirmed_state, message=message,
        )
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=order_result,
            interpretation=interpretation,
            state=evolve_commerce_state(confirmed_state, order_result),
        )
    if interpretation is not None and interpretation.checkout_data is not None:
        checkout_updates = interpretation.checkout_data.model_dump(
            mode="json",
            exclude_none=True,
        )
        checkout_result = update_checkout_data(
            state,
            checkout_updates,
        )
        field_errors = checkout_result.commercial_data.get("field_errors")
        field_errors = field_errors if isinstance(field_errors, dict) else {}
        missing_fields = checkout_result.commercial_data.get("missing_fields")
        missing_fields = missing_fields if isinstance(missing_fields, list) else []
        enriched_updates = await enrich_checkout_data_from_cep(
            checkout_updates,
            known_zipcode=state.checkout_draft.address.zip_code,
            missing_fields=missing_fields,
            field_errors=field_errors,
        )
        cep_resolution_applied = enriched_updates != checkout_updates
        if cep_resolution_applied:
            checkout_updates = enriched_updates
            checkout_result = update_checkout_data(state, checkout_updates)
            field_errors = checkout_result.commercial_data.get("field_errors")
            field_errors = field_errors if isinstance(field_errors, dict) else {}
            missing_fields = checkout_result.commercial_data.get("missing_fields")
            missing_fields = missing_fields if isinstance(missing_fields, list) else []
        repair_attempted = should_repair_checkout_data(
            message.text,
            checkout_updates,
            missing_fields,
            field_errors,
        )
        if repair_attempted:
            repaired_updates = await repair_checkout_data_with_openai(
                message_text=message.text,
                updates=checkout_updates,
                missing_fields=missing_fields,
                field_errors=field_errors,
            )
            if repaired_updates != checkout_updates:
                checkout_result = update_checkout_data(state, repaired_updates)
            checkout_result.response_metadata["checkout_data_repair_attempted"] = True
            checkout_result.response_metadata["checkout_data_repair_applied"] = (
                repaired_updates != checkout_updates
            )
        checkout_result.response_metadata["checkout_cep_resolution_applied"] = (
            cep_resolution_applied
        )
        payment_preference = (
            interpretation.payment_method_preference
            or state.payment_method_preference
        )
        if payment_preference is not None:
            checkout_result.response_metadata["payment_method_preference"] = (
                payment_preference
            )
        checkout_result = await _advance_whatsapp_checkout(
            state,
            checkout_result,
            payment_preference,
            interpretation.installment_count,
        )
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=checkout_result,
            interpretation=interpretation,
            state=evolve_commerce_state(state, checkout_result),
        )
    if interpretation is not None and interpretation.shipping_action == "quote":
        shipping_result = await quote_shipping(
            state=state,
            zipcode=interpretation.shipping_zipcode or "",
            execute=execute_tool,
        )
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=shipping_result,
            interpretation=interpretation,
        )
    if interpretation is not None and interpretation.shipping_action == "list_methods":
        shipping_result = await list_shipping_methods(execute=execute_tool)
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=shipping_result,
            interpretation=interpretation,
        )
    if interpretation is not None and interpretation.shipping_action == "select":
        shipping_result = select_shipping(
            state,
            selection_id=interpretation.shipping_selection_id,
            selection_position=interpretation.shipping_selection_position,
        )
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=shipping_result,
            interpretation=interpretation,
        )
    if (
        interpretation is not None
        and interpretation.confirmation == "confirm"
        and state.pending_action == "awaiting_shipping_selection"
        and len(state.shipping_quotes) == 1
    ):
        shipping_result = select_shipping(state, selection_position=1)
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=shipping_result,
            interpretation=interpretation,
        )
    if interpretation is not None and interpretation.checkout_action == "prepare_order":
        order_result = await prepare_order(state=state, execute=execute_tool)
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=order_result,
            interpretation=interpretation,
        )
    if interpretation is not None and interpretation.checkout_action == "create_order":
        order_result = await _fulfill_confirmed_order(state, message=message)
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=order_result,
            interpretation=interpretation,
            state=evolve_commerce_state(state, order_result),
        )
    if (
        interpretation is not None
        and state.pending_action
        and interpretation.confirmation == "reject"
    ):
        return _pending_action_rejected_result(interpretation, state)
    if (
        interpretation is not None
        and state.pending_action
        and interpretation.confirmation == "none"
    ):
        interpretation._clear_pending_action = True
    resolved_product = None
    resolved_by = "none"
    if interpretation is not None:
        log_purchase_progress("reference_resolution", "start")
        resolved_product, resolved_by = resolve_commerce_reference(interpretation, state)
        log_purchase_progress(
            "reference_resolution",
            "success" if resolved_product is not None else "blocked",
            None if resolved_product is not None else "reference_not_resolved",
        )
        print("[sales.reference]", {
            "type": interpretation.reference_type,
            "position": interpretation.reference_position,
            "resolved": resolved_product is not None,
            "resolved_by": resolved_by,
        })
        # "Quero ver um Seiko" is a fresh browse — never treat prior shortlist as
        # the target SKU (that skipped qualification and hammered Tray get_product).
        if (
            resolved_product is not None
            and interpretation.reference_type
            in {
                "previous_recommendation",
                "last_presented_product",
                "current_product",
            }
            and is_open_catalog_browse_request(message.text, interpretation)
        ):
            print(
                "[sales.reference.ignore_stale_browse]",
                {
                    "reference_type": interpretation.reference_type,
                    "product_id": resolved_product.product_id,
                    "brand": interpretation.subject.brand,
                },
            )
            resolved_product = None
            resolved_by = "none"
            interpretation = interpretation.model_copy(
                update={"reference_type": None, "reference_position": None}
            )
        # Inbound photo must re-identify — never answer price from a stale
        # Kingfisher/sibling left in active/presented context.
        from .image_product_id import (
            handle_image_product_search,
            image_search_eligible,
        )
        from .commerce_router import is_deictic_product_price_request
        from .brevo_instagram_media import (
            PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
            UNVIEWABLE_MEDIA_GUIDE_REPLY,
            is_bare_price_request,
            is_brevo_unviewable_media_text,
            should_guide_instagram_price_without_media,
        )

        vague_refs = {
            None,
            "none",
            "current_product",
            "last_presented_product",
            "previous_recommendation",
        }
        has_inbound_image = bool((message.image_url or "").strip())
        if is_brevo_unviewable_media_text(message.text):
            return _mark_sales_result(
                AgentResult(
                    reply_text=UNVIEWABLE_MEDIA_GUIDE_REPLY,
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="instagram_media_unviewable",
                    response_metadata={"domain": "commerce"},
                ),
                interpretation=interpretation,
                goal=interpretation.goal,
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="brevo_instagram_media_unviewable",
            )
        # Instagram Story / unsupported IG media never arrives with image_url via
        # Brevo. Bare "valor" after that must not invent a SKU or ask vaguely.
        if (
            not has_inbound_image
            and should_guide_instagram_price_without_media(message)
            and interpretation.reference_type in vague_refs
            and interpretation.goal in {"inspect", "find", "discover", "recommend"}
            and resolved_product is None
            and state.active_product is None
        ):
            print("[sales.instagram.price_without_media]", {
                "bare_price": is_bare_price_request(message.text),
                "channel": message.channel,
            })
            return _mark_sales_result(
                AgentResult(
                    reply_text=PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="instagram_media_unviewable",
                    response_metadata={"domain": "commerce"},
                ),
                interpretation=interpretation,
                goal=interpretation.goal,
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="instagram_price_without_media",
            )
        # Brevo often splits photo+caption: text "qual o preço desse?" arrives
        # without image_url and would price the previous SKU (CW Rosa → Beaubleu).
        if (
            not has_inbound_image
            and is_deictic_product_price_request(message.text)
            and interpretation.reference_type in vague_refs
            and interpretation.goal in {"inspect", "find"}
        ):
            print("[sales.reference.ignore_stale_deictic]", {
                "had_resolved": resolved_product is not None,
                "reference_type": interpretation.reference_type,
                "active_product_id": (
                    state.active_product.product_id if state.active_product else None
                ),
            })
            resolved_product = None
            resolved_by = "none"
            if state.active_product is not None:
                state.active_product = None
            # Caption-only fragment before the photo lands — wait for the image
            # instead of quoting the previous watch.
            if not (
                state.product_resolution_state == "plausible_matches"
                and state.last_presented_products
            ):
                return _mark_sales_result(
                    AgentResult(
                        reply_text=(
                            "Recebi sua pergunta de preço. Se for o relógio da foto, "
                            "me envia a imagem (ou a marca e o modelo) que eu confirmo "
                            "no catálogo e te passo o valor certinho."
                        ),
                        intent="commerce",
                        handoff_required=False,
                        safety_reason="product_context_missing",
                        response_metadata={
                            "domain": "commerce",
                            "clear_active_product": True,
                        },
                    ),
                    interpretation=interpretation,
                    goal=interpretation.goal,
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=False,
                    fallback_reason="deictic_price_without_image",
                )
        if (
            image_search_eligible(message)
            and interpretation.reference_type in vague_refs
            and interpretation.goal in {
                "find",
                "inspect",
                "recommend",
                "discover",
                "compare",
            }
        ):
            image_result = await handle_image_product_search(message)
            if image_result is not None:
                return _mark_sales_result(
                    image_result,
                    interpretation=interpretation,
                    goal="find",
                    response_source=image_result.response_metadata.get(
                        "response_source",
                        "image_vision",
                    ),
                    used_openai_responder=bool(
                        image_result.response_metadata.get("used_openai_responder")
                    ),
                    used_tray=bool(
                        image_result.response_metadata.get("used_tray")
                        or (image_result.commercial_data or {}).get("products")
                    ),
                    fallback_reason=image_result.safety_reason,
                )
        # Soft nearby siblings are not a confirmed product for "qual o preço?".
        # Follow-ups about the listed models (pronta entrega, todos, desses)
        # must stay on that list — never dump a stale photo-match catalog.
        if (
            not interpretation.image_request
            and not is_outbound_catalog_image_request(message.text)
            and is_listed_catalog_follow_up(message.text)
            and state.last_presented_products
            and not (message.image_url or "").strip()
        ):
            inspect_result = await _inspect_listed_products(state)
            final = await _sales_response_with_openai(
                message,
                plan,
                inspect_result,
                interpretation,
                state=state,
            )
            if final:
                return final
            return _mark_sales_result(
                inspect_result,
                interpretation=interpretation,
                goal=interpretation.goal,
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=True,
                fallback_reason=inspect_result.safety_reason,
            )
        if (
            not interpretation.image_request
            and not is_outbound_catalog_image_request(message.text)
            and not is_listed_catalog_follow_up(message.text)
            and state.product_resolution_state == "plausible_matches"
            and interpretation.goal == "inspect"
            and interpretation.reference_type in vague_refs
            and state.last_presented_products
        ):
            from .commerce_router import _product_lines

            numbered = [
                f"{position}. {line}"
                for position, line in enumerate(
                    _product_lines(
                        [
                            item.model_dump(mode="json")
                            for item in state.last_presented_products[:3]
                        ],
                        compact=True,
                    ),
                    start=1,
                )
            ]
            return _mark_sales_result(
                AgentResult(
                    reply_text=(
                        "Ainda não confirmei o modelo exato. Destes que listei, "
                        "qual você quer o preço?\n"
                        + "\n".join(numbered)
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="exact_product_ambiguous_brand",
                    commercial_data={
                        "products": [
                            item.model_dump(mode="json")
                            for item in state.last_presented_products[:3]
                        ],
                        "match_status": "ambiguous",
                    },
                    response_metadata={
                        "presented_products": True,
                        "product_resolution_state": "plausible_matches",
                        "clear_active_product": True,
                        "domain": "commerce",
                    },
                ),
                interpretation=interpretation,
                goal=interpretation.goal,
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="plausible_matches_price_blocked",
            )
    purchase_action = interpretation.purchase_action if interpretation is not None else None
    if purchase_action == "remove_cart_item":
        if not state.cart_session_id:
            removal_result = AgentResult(
                reply_text="",
                intent="commerce",
                safety_reason="cart_item_removal_cart_invalid",
                commercial_data={
                    "removal_requested": True,
                    "removal_supported": False,
                },
                response_metadata={
                    "domain": "commerce",
                    "clear_pending_action": True,
                    "used_tray": False,
                },
            )
            return await _respond_to_commerce_service(
                message=message,
                plan=plan,
                result=removal_result,
                interpretation=interpretation,
            )
        targets, resolution_reason = resolve_cart_item_reference(interpretation, state, resolved_product)
        if not targets:
            if resolution_reason == "ambiguous":
                removal_result = AgentResult(
                    reply_text="",
                    intent="commerce",
                    safety_reason="cart_item_removal_ambiguous",
                    commercial_data={
                        "removal_requested": True,
                        "removal_supported": True,
                        "mutation_success": False,
                        "reason": "ambiguous",
                        "cart_items": [
                            {
                                "position": i + 1,
                                "product_id": item.product_id,
                                "variant_id": item.variant_id,
                                "name": item.name,
                                "quantity": item.quantity,
                            }
                            for i, item in enumerate(state.cart_items)
                        ],
                    },
                    response_metadata={
                        "domain": "commerce",
                        "clear_pending_action": True,
                        "used_tray": False,
                    },
                )
            else:
                removal_result = AgentResult(
                    reply_text="",
                    intent="commerce",
                    safety_reason="cart_item_removal_not_found",
                    commercial_data={
                        "removal_requested": True,
                        "removal_supported": True,
                        "mutation_success": False,
                        "reason": resolution_reason,
                        "cart_items": [
                            {
                                "position": i + 1,
                                "product_id": item.product_id,
                                "variant_id": item.variant_id,
                                "name": item.name,
                                "quantity": item.quantity,
                            }
                            for i, item in enumerate(state.cart_items)
                        ],
                    },
                    response_metadata={
                        "domain": "commerce",
                        "clear_pending_action": True,
                        "used_tray": False,
                    },
                )
            return await _respond_to_commerce_service(
                message=message,
                plan=plan,
                result=removal_result,
                interpretation=interpretation,
            )
        new_state, rebuild_result = await rebuild_cart_without(state, targets, execute_tool)
        if rebuild_result.get("success"):
            final_state = new_state
            removal_result = AgentResult(
                reply_text="",
                intent="commerce",
                commercial_data={
                    "removal_requested": True,
                    "removal_supported": True,
                    "mutation_success": True,
                    "cart_items": [
                        {
                            "product_id": item.product_id,
                            "variant_id": item.variant_id,
                            "quantity": item.quantity,
                        }
                        for item in final_state.cart_items
                    ],
                    "shipping_reset": True,
                    "payment_reset": True,
                },
                response_metadata={
                    "domain": "commerce",
                    "cart_state": {
                        "cart_session_id": final_state.cart_session_id,
                        "cart_items": [item.model_dump(mode="json") for item in final_state.cart_items],
                    } if final_state.cart_session_id else {"cart_session_id": None, "cart_items": []},
                    "purchase_stage": "shopping",
                    "clear_pending_action": True,
                    "used_tray": True,
                },
            )
            state = final_state
        else:
            reason = rebuild_result.get("reason", "unknown")
            is_partial = rebuild_result.get("partial_rebuild", False)
            if reason == "item_not_found":
                safety_reason = "cart_item_removal_item_not_found"
            elif is_partial:
                safety_reason = "cart_item_removal_partial"
            elif reason == "delete_failed":
                safety_reason = "cart_item_removal_delete_failed"
            else:
                safety_reason = "cart_item_removal_failed"
            removal_result = AgentResult(
                reply_text="",
                intent="commerce",
                safety_reason=safety_reason,
                commercial_data={
                    "removal_requested": True,
                    "removal_supported": True,
                    "mutation_success": False,
                    "reason": reason,
                    "cart_items": [
                        {
                            "product_id": item.product_id,
                            "variant_id": item.variant_id,
                            "quantity": item.quantity,
                        }
                        for item in (new_state.cart_items if is_partial else state.cart_items)
                    ] if is_partial or rebuild_result.get("success") is False else [],
                },
                response_metadata={
                    "domain": "commerce",
                    "clear_pending_action": True,
                    "used_tray": True,
                },
            )
        return await _respond_to_commerce_service(
            message=message,
            plan=plan,
            result=removal_result,
            interpretation=interpretation,
        )
    purchase_requests: list[CartItemRequest] = []
    unresolved_purchase_items = 0
    unresolved_candidates: list[dict[str, Any]] = []
    pending_link_requested = bool(
        interpretation
        and interpretation.product_action == "get_product_link"
    )
    pending_action_used = False
    if (
        interpretation is not None
        and state.pending_action == "show_nearby_line"
        and (
            interpretation.confirmation == "confirm"
            or is_short_affirmation(message.text)
        )
    ):
        from .image_product_id import (
            ImageProductIdentification,
            soft_line_interpretation_from_identification,
        )

        prefs = state.active_preferences if isinstance(state.active_preferences, dict) else {}
        identify_payload = prefs.get("image_identify")
        soft_interp = None
        if isinstance(identify_payload, dict):
            try:
                soft_interp = soft_line_interpretation_from_identification(
                    ImageProductIdentification.model_validate(identify_payload)
                )
            except (TypeError, ValueError):
                soft_interp = None
        if soft_interp is None:
            brand = prefs.get("nearby_line_brand") or (
                state.active_product.brand if state.active_product else None
            )
            model = prefs.get("nearby_line_model")
            if brand or model:
                soft_interp = SalesInterpretation(
                    domain="commerce",
                    goal="recommend",
                    subject={
                        "product_type": "relógio",
                        "brand": brand,
                        "model": model,
                    },
                    preferences={
                        "color": prefs.get("nearby_line_color"),
                    },
                    information_needed=["catalog"],
                    references_previous_context=True,
                    enough_information_to_search=True,
                    ready_for_retrieval=True,
                    stop_clarification=True,
                    needs_clarification=False,
                    clarification_question=None,
                    confidence=0.8,
                    active_topic="nearby_line_options",
                    purchase_stage="discovery",
                    confirmation="confirm",
                )
                soft_interp._force_recommendation_mode = True
                soft_interp._source = "nearby_line_resume"
        if soft_interp is not None:
            print("[sales.pending_action]", {
                "action": "show_nearby_line",
                "confirmed": True,
                "brand": soft_interp.subject.brand,
                "model": soft_interp.subject.model,
            })
            tray_result = await _execute_compiled_product_retrieval(
                soft_interp,
                message_text=message.text,
            )
            interpretation._clear_pending_action = True
            pending_action_used = True
            if tray_result is not None:
                return _mark_sales_result(
                    tray_result,
                    interpretation=soft_interp,
                    goal="recommend",
                    response_source=tray_result.response_metadata.get(
                        "response_source",
                        "nearby_line_resume",
                    ),
                    used_openai_responder=False,
                    used_tray=True,
                    fallback_reason=tray_result.safety_reason,
                )
            return _mark_sales_result(
                AgentResult(
                    reply_text=(
                        "Ainda não achei opções dessa linha no catálogo agora. "
                        "Se tiver a referência do modelo, me manda que eu confiro."
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="product_not_found",
                    response_metadata={
                        "domain": "commerce",
                        "clear_pending_action": True,
                    },
                ),
                interpretation=soft_interp,
                goal="recommend",
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=True,
                fallback_reason="nearby_line_empty",
            )
    if (
        interpretation is not None
        and state.pending_action
        and state.pending_action != "show_nearby_line"
        and interpretation.confirmation == "confirm"
        and interpretation.goal in {"discover", "find", "recommend", "compare"}
    ):
        # A new explicit discovery request is not confirmation of an old checkout step.
        interpretation._clear_pending_action = True
    if (
        interpretation is not None
        and state.pending_action
        and interpretation.confirmation == "confirm"
        and purchase_action not in {"set_cart_item_quantity", "remove_cart_item"}
        and interpretation.payment_action is None
        and interpretation.checkout_data is None
        and interpretation.shipping_action is None
        and interpretation.goal not in {"discover", "find", "recommend", "compare"}
        and not (
            purchase_action == "create_cart"
            and interpretation.reference_type in {"list_position", "explicit_product"}
        )
    ):
        pending_references = _pending_product_references(state)
        pending_action = state.pending_action
        interpretation._clear_pending_action = True
        if pending_action in {"create_cart", "confirm_purchase"}:
            purchase_action = "create_cart"
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
            elif pending_references:
                purchase_requests.extend(
                    CartItemRequest(
                        product_reference=reference,
                        quantity=interpretation.quantity or 1,
                        resolved_from="pending_action",
                        variant_preferences=interpretation.preferences.model_dump(
                            mode="json",
                            exclude_none=True,
                        ),
                    )
                    for reference in pending_references
                )
        elif pending_action == "show_images":
            interpretation = interpretation.model_copy(update={"image_request": True})
            interpretation._clear_pending_action = True
            plan = interpretation_to_plan(interpretation, message.text)
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        elif pending_action == "show_payment_options":
            interpretation = interpretation.model_copy(
                update={
                    "payment_action": "payment_options",
                    "purchase_action": (
                        "create_cart"
                        if not state.cart_session_id and len(pending_references) == 1
                        else interpretation.purchase_action
                    ),
                }
            )
            interpretation._clear_pending_action = True
            plan = interpretation_to_plan(interpretation, message.text)
            purchase_action = interpretation.purchase_action
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        elif pending_action == "send_product_link":
            pending_link_requested = True
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        elif pending_action == "choose_checkout_channel":
            interpretation._clear_pending_action = bool(
                interpretation.checkout_channel_preference
            )
            pending_action_used = bool(
                interpretation.checkout_channel_preference
            )
        elif pending_action == "awaiting_shipping_zipcode":
            interpretation._clear_pending_action = False
            requirement_result = AgentResult(
                reply_text="Ainda existe um requisito factual de entrega pendente.",
                intent="commerce",
                handoff_required=False,
                safety_reason="checkout_requirements_missing",
                commercial_data={
                    "checkout_ready_for_payment": False,
                    "missing_checkout_requirements": ["shipping_zipcode"],
                    "checkout_blockers": ["shipping_zipcode_missing"],
                },
                response_metadata={
                    "domain": "commerce",
                    "purchase_stage": "shipping",
                    "pending_action": "awaiting_shipping_zipcode",
                    "pending_action_product_ids": [],
                    "used_tray": False,
                },
            )
            return await _respond_to_commerce_service(
                message=message,
                plan=plan,
                result=requirement_result,
                interpretation=interpretation,
            )
        pending_action_used = bool(
            pending_action_used
            or pending_references
            or (
                pending_action == "show_payment_options"
                and state.cart_session_id
            )
        )
        print("[sales.pending_action]", {
            "action": pending_action,
            "has_product": bool(pending_references),
            "confirmation": interpretation.confirmation,
            "executed": bool(
                resolved_product
                or purchase_requests
                or pending_action == "show_payment_options"
                or (
                    pending_action == "choose_checkout_channel"
                    and interpretation.checkout_channel_preference
                )
            ),
        })
    if interpretation is not None:
        print("[sales.state.application]", {
            "had_pending_action": bool(state.pending_action),
            "pending_action_used": pending_action_used,
            "pending_action_cleared": interpretation._clear_pending_action,
            "had_active_product": state.active_product is not None,
            "active_product_referenced": bool(
                interpretation.reference_type == "current_product"
                and resolved_product is not None
            ),
        })
    if interpretation is not None and interpretation.purchase_items:
        for item in interpretation.purchase_items:
            log_purchase_progress("reference_resolution", "start")
            reference, item_resolved_by = resolve_purchase_item_reference(item, state)
            log_purchase_progress(
                "reference_resolution",
                "success" if reference is not None else "blocked",
                None if reference is not None else "purchase_item_not_resolved",
            )
            if (
                reference is None
                and item.reference_type == "explicit_product"
                and item.explicit_product_name
            ):
                log_purchase_progress("product_resolution", "start")
                item_subject = interpretation.subject.model_copy(update={
                    "model": item.explicit_product_name,
                    "reference": None,
                    "ean": None,
                })
                item_interpretation = interpretation.model_copy(
                    deep=True,
                    update={
                        "goal": "find",
                        "subject": item_subject,
                        "purchase_action": None,
                        "purchase_items": [],
                        "quantity": None,
                        "needs_clarification": False,
                        "ready_for_retrieval": True,
                    },
                )
                lookup = await _execute_compiled_product_retrieval(
                    item_interpretation,
                    message_text=message.text,
                )
                candidates = (
                    (lookup.commercial_data or {}).get("products")
                    if lookup is not None
                    else None
                )
                candidates = candidates if isinstance(candidates, list) else []
                if len(candidates) == 1 and isinstance(candidates[0], dict):
                    reference = product_reference_from_product(candidates[0])
                    item_resolved_by = "explicit_product"
                    log_purchase_progress("product_resolution", "success")
                elif candidates:
                    unresolved_candidates = [
                        candidate
                        for candidate in candidates[:3]
                        if isinstance(candidate, dict)
                    ]
                    log_purchase_progress(
                        "product_resolution",
                        "blocked",
                        "ambiguous_purchase_item",
                    )
                else:
                    log_purchase_progress(
                        "product_resolution",
                        (
                            "failed"
                            if lookup is not None and lookup.safety_reason
                            else "blocked"
                        ),
                        (
                            lookup.safety_reason
                            if lookup is not None and lookup.safety_reason
                            else "product_not_found"
                        ),
                    )
            if reference is None:
                unresolved_purchase_items += 1
                continue
            purchase_requests.append(CartItemRequest(
                product_reference=reference,
                quantity=item.quantity,
                position=item.reference_position,
                resolved_from=item_resolved_by,
                variant_preferences=interpretation.preferences.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            ))
        print("[sales.cart.items]", {
            "requested_count": len(interpretation.purchase_items),
            "resolved_count": len(purchase_requests),
        })
    if (
        interpretation is not None
        and (
            purchase_action == "create_cart"
            or interpretation.product_action == "get_product_link"
        )
        and not purchase_requests
        and resolved_product is None
        and any((
            interpretation.subject.reference,
            interpretation.subject.ean,
            interpretation.subject.model,
        ))
    ):
        log_purchase_progress("product_resolution", "start")
        lookup = await _execute_compiled_product_retrieval(
            interpretation,
            message_text=message.text,
        )
        lookup_products = (
            (lookup.commercial_data or {}).get("products")
            if lookup is not None
            else None
        )
        lookup_products = lookup_products if isinstance(lookup_products, list) else []
        if len(lookup_products) == 1 and isinstance(lookup_products[0], dict):
            resolved_product = product_reference_from_product(lookup_products[0])
            resolved_by = "product_id"
            log_purchase_progress("product_resolution", "success")
        elif lookup_products:
            unresolved_purchase_items = 1
            unresolved_candidates = [
                candidate
                for candidate in lookup_products[:3]
                if isinstance(candidate, dict)
            ]
            log_purchase_progress(
                "product_resolution",
                "blocked",
                "ambiguous_purchase_item",
            )
        elif lookup is not None:
            log_purchase_progress(
                "product_resolution",
                "failed" if lookup.safety_reason else "blocked",
                lookup.safety_reason or "product_not_found",
            )
            return _mark_sales_result(
                lookup,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source=(
                    "technical_fallback"
                    if lookup.safety_reason in {
                        "tray_adapter_unavailable",
                        "product_match_failed",
                    }
                    else "deterministic_fallback"
                ),
                used_openai_responder=False,
                used_tray=bool(lookup.response_metadata.get("used_tray", True)),
                fallback_reason=lookup.safety_reason,
            )
    if (
        interpretation is not None
        and interpretation.checkout_channel_preference is not None
        and interpretation.purchase_action != "create_cart"
        and interpretation.payment_action is None
    ):
        channel_result = select_checkout_channel(
            state,
            interpretation.checkout_channel_preference,
        )
        if (
            interpretation.checkout_channel_preference == "whatsapp"
            and state.checkout_channel_preference == "whatsapp"
        ):
            channel_result = await _advance_whatsapp_checkout(
                state,
                channel_result,
                state.payment_method_preference,
                interpretation.installment_count,
            )
        final = await _sales_response_with_openai(
            message,
            plan,
            channel_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            channel_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=channel_result.safety_reason,
        )
    if interpretation is not None and interpretation.image_request:
        listed_refs = [
            CommerceProductReference.model_validate(
                item.model_dump(exclude={"position"})
            )
            for item in state.last_presented_products[:3]
        ]
        send_listed = False
        if interpretation.reference_position is not None:
            positioned = [
                CommerceProductReference.model_validate(
                    item.model_dump(exclude={"position"})
                )
                for item in state.last_presented_products
                if item.position == interpretation.reference_position
            ]
            if positioned:
                listed_refs = positioned
                send_listed = True
        elif listed_refs and not (message.image_url or "").strip() and (
            wants_all_listed_product_images(message.text)
            or resolved_product is None
        ):
            send_listed = True
        print("[sales.action.guard]", {
            "action": "show_images",
            "target_count": (
                len(listed_refs)
                if send_listed
                else (1 if resolved_product is not None else 0)
            ),
            "allowed": send_listed or resolved_product is not None,
            "blocking_reason": (
                None
                if send_listed or resolved_product is not None
                else "product_target_missing"
            ),
            "inbound_image": bool((message.image_url or "").strip()),
        })
        if send_listed:
            media_result = await resolve_presented_product_images(
                product_references=listed_refs,
                execute=execute_tool,
            )
            final = await _sales_response_with_openai(
                message,
                plan,
                media_result,
                interpretation,
            )
            if final:
                return final
            return _mark_sales_result(
                media_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source=(
                    "technical_fallback"
                    if media_result.safety_reason == "product_media_technical_failure"
                    else "deterministic_fallback"
                ),
                used_openai_responder=False,
                used_tray=True,
                fallback_reason=media_result.safety_reason,
            )
        if resolved_product is None:
            # Customer sent a product photo — identify it, don't ask for the name first.
            from .image_product_id import (
                handle_image_product_search,
                image_search_eligible,
            )

            if image_search_eligible(message):
                image_result = await handle_image_product_search(message)
                if image_result is not None:
                    return _mark_sales_result(
                        image_result,
                        interpretation=interpretation,
                        goal="find",
                        response_source=image_result.response_metadata.get(
                            "response_source",
                            "image_vision",
                        ),
                        used_openai_responder=bool(
                            image_result.response_metadata.get("used_openai_responder")
                        ),
                        used_tray=bool(
                            image_result.response_metadata.get("used_tray")
                            or (image_result.commercial_data or {}).get("products")
                        ),
                        fallback_reason=image_result.safety_reason,
                    )
            return _mark_sales_result(
                AgentResult(
                    reply_text=(
                        "Pode me enviar a foto do relógio (ou a marca e o modelo) "
                        "que eu identifico no catálogo pra você?"
                        if not (message.image_url or "").strip()
                        else (
                            "Recebi a foto, mas não consegui identificar o produto agora. "
                            "Pode me dizer a marca e o modelo, ou enviar uma imagem mais nítida?"
                        )
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="product_context_missing",
                ),
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
            )
        media_result = await resolve_product_image(
            product_reference=resolved_product,
            execute=execute_tool,
        )
        final = await _sales_response_with_openai(
            message,
            plan,
            media_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            media_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if media_result.safety_reason == "product_media_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=True,
            fallback_reason=media_result.safety_reason,
        )
    if interpretation is not None and pending_link_requested:
        if resolved_product is None:
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )
        link_facts = await execute_tool(
            "get_product_link",
            {"product_id": resolved_product.product_id},
        )
        link_failed = "error" in link_facts
        product_url = link_facts.get("product_url")
        url_dead = bool(link_facts.get("product_url_dead")) or not isinstance(
            product_url, str
        )
        if url_dead and not link_failed:
            reply_text = (
                "Consultei o produto na Tray, mas o link da vitrine deste acabamento "
                "está inconsistente no site agora. Posso te passar a referência/valor "
                "ou encaminhar para o atendimento fechar o pedido."
            )
            safety = "product_link_not_available"
        elif link_failed:
            reply_text = "Não consegui consultar o link oficial deste produto agora."
            safety = "tray_adapter_unavailable"
        else:
            reply_text = f"Link oficial consultado.\n{product_url}"
            safety = None
        link_result = AgentResult(
            reply_text=reply_text,
            intent="commerce",
            handoff_required=False,
            safety_reason=safety,
            commercial_data={
                "product_link": {
                    "product_id": link_facts.get("product_id")
                    or resolved_product.product_id,
                    "product_name": link_facts.get("product_name")
                    or resolved_product.name,
                    "product_url": product_url,
                    "product_url_repaired": bool(
                        link_facts.get("product_url_repaired")
                    ),
                    "product_url_dead": url_dead,
                }
            },
            response_metadata={
                "domain": "commerce",
                "active_product": resolved_product.model_dump(mode="json"),
                "clear_pending_action": True,
                "used_tray": True,
            },
        )
        final = await _sales_response_with_openai(
            message,
            plan,
            link_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            link_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if link_result.safety_reason == "tray_adapter_unavailable"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=True,
            fallback_reason=link_result.safety_reason,
        )
    if purchase_action == "set_cart_item_quantity":
        if resolved_product is None:
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )
        if interpretation.quantity is None:
            quantity_result = AgentResult(
                reply_text="A quantidade final não foi identificada.",
                intent="commerce",
                handoff_required=False,
                safety_reason="cart_validation_error",
                commercial_data={
                    "cart": {"mutation_success": False},
                },
                response_metadata={"domain": "commerce", "used_tray": False},
            )
        else:
            quantity_result = await set_cart_item_quantity(
                product_reference=resolved_product,
                quantity=interpretation.quantity,
                state=state,
                execute=execute_tool,
            )
        if (
            quantity_result.response_metadata.get("cart_materially_changed") is True
            and state.checkout_channel_preference == "whatsapp"
        ):
            updated_state = evolve_commerce_state(state, quantity_result)
            known_zipcode = updated_state.checkout_draft.address.zip_code
            if known_zipcode:
                quote_result = await quote_shipping(
                    state=updated_state,
                    zipcode=known_zipcode,
                    execute=execute_tool,
                )
                quantity_result = _combine_checkout_and_followup_results(
                    quantity_result,
                    quote_result,
                )
        final = await _sales_response_with_openai(
            message,
            plan,
            quantity_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            quantity_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=bool(quantity_result.response_metadata.get("used_tray")),
            fallback_reason=quantity_result.safety_reason,
        )
    payment_preference = (
        interpretation.payment_method_preference
        if interpretation is not None
        else None
    )
    payment_requested = bool(
        interpretation
        and (interpretation.payment_action or payment_preference)
    )
    if purchase_action == "create_cart":
        target_count = len(purchase_requests) + (
            1 if resolved_product is not None else 0
        )
        print("[sales.action.guard]", {
            "action": "create_cart",
            "target_count": target_count,
            "allowed": target_count > 0,
            "blocking_reason": (
                None if target_count > 0 else "product_target_missing"
            ),
        })
    needs_cart = bool(
        payment_requested
        and (
            purchase_action in {"create_cart", "checkout_question", "show_cart_link"}
            or purchase_requests
        )
        and (
            not (state.cart_session_id and state.cart_url)
            or purchase_action == "create_cart"
            or bool(purchase_requests)
        )
    )
    print("[sales.purchase.orchestrator.decision]", {
        "intent": plan.get("intent"),
        "purchase_action": purchase_action,
        "has_active_product": state.active_product is not None,
        "purchase_item_count": len(purchase_requests),
        "has_cart_session": bool(state.cart_session_id),
        "needs_cart": needs_cart,
        "payment_requested": payment_requested,
    })
    if (
        not payment_requested
        and purchase_action in {"checkout_question", "show_cart_link"}
        and not state.cart_session_id
    ):
        if unresolved_purchase_items or (
            not purchase_requests and resolved_product is None
        ):
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )
        _ensured_state, ensured_result = await _ensure_cart_for_purchase(
            interpretation=interpretation,
            state=state,
            purchase_requests=purchase_requests,
            resolved_product=resolved_product,
        )
        if ensured_result is not None:
            final = await _sales_response_with_openai(
                message,
                plan,
                ensured_result,
                interpretation,
            )
            if final:
                return final
            return _mark_sales_result(
                ensured_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source=(
                    "technical_fallback"
                    if ensured_result.safety_reason == "cart_technical_failure"
                    else "deterministic_fallback"
                ),
                used_openai_responder=False,
                used_tray=bool(ensured_result.response_metadata.get("used_tray", True)),
                fallback_reason=ensured_result.safety_reason,
            )
    if payment_requested:
        informational_payment = _is_informational_payment_query(
            interpretation,
            purchase_action=purchase_action,
            has_purchase_requests=bool(purchase_requests),
        )
        if informational_payment and not (
            state.cart_session_id and state.cart_url
        ):
            from .persona_runtime import get_persona_runtime

            runtime = get_persona_runtime()
            force_cart = bool(
                runtime and runtime.require_cart_for_informational_payment
            )
            if force_cart:
                missing = _purchase_product_required_result(state)
                return _mark_sales_result(
                    missing,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=False,
                    fallback_reason=missing.safety_reason,
                )
            print("[sales.purchase.orchestrator.decision]", {
                "branch": "informational_payment_no_cart",
                "payment_request_kind": (
                    interpretation.payment_request_kind
                    if interpretation is not None
                    else None
                ),
                "persona_policy_source": (
                    runtime.policy_source if runtime is not None else None
                ),
            })
            info_result = _informational_payment_policy_result(
                state,
                payment_method_preference=payment_preference,
            )
            final = await _sales_response_with_openai(
                message,
                plan,
                info_result,
                interpretation,
            )
            if final:
                return final
            return _mark_sales_result(
                info_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=info_result.safety_reason,
            )

        if unresolved_purchase_items:
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )

        payment_state = state
        cart_result: AgentResult | None = None
        if needs_cart:
            payment_state, cart_result = await _ensure_cart_for_purchase(
                interpretation=interpretation,
                state=state,
                purchase_requests=purchase_requests,
                resolved_product=resolved_product,
            )
            if cart_result is not None and not (
                payment_state.cart_session_id
                and payment_state.cart_url
            ):
                return _mark_sales_result(
                    cart_result,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source=(
                        "technical_fallback"
                        if cart_result.safety_reason == "cart_technical_failure"
                        else "deterministic_fallback"
                    ),
                    used_openai_responder=False,
                    used_tray=bool(cart_result.response_metadata.get("used_tray", True)),
                    fallback_reason=cart_result.safety_reason,
                )
        if not (
            payment_state.cart_session_id
            and payment_state.cart_url
        ):
            from .persona_runtime import get_persona_runtime

            runtime = get_persona_runtime()
            if runtime is not None and not runtime.require_product_before_checkout:
                info_result = _informational_payment_policy_result(
                    state,
                    payment_method_preference=payment_preference,
                )
                final = await _sales_response_with_openai(
                    message,
                    plan,
                    info_result,
                    interpretation,
                )
                if final:
                    return final
                return _mark_sales_result(
                    info_result,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=False,
                    fallback_reason=info_result.safety_reason,
                )
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )

        payment_result = await inspect_payment_options(
            state=payment_state,
            installment_count=interpretation.installment_count,
            payment_method_preference=payment_preference,
            execute=execute_tool,
            payment_option_id=interpretation.payment_option_id,
            advance_checkout=bool(
                payment_preference is not None
                or interpretation.payment_request_kind == "checkout"
                or purchase_action == "checkout_question"
            ),
            reconciled_cart=(
                {
                    **((cart_result.commercial_data or {}).get("cart") or {}),
                    "items": (
                        (cart_result.response_metadata.get("cart_state") or {}).get(
                            "cart_items", []
                        )
                    ),
                }
                if cart_result is not None
                else None
            ),
        )
        if payment_preference is not None:
            payment_result.response_metadata["payment_method_preference"] = (
                payment_preference
            )
        combined_result = (
            _combine_cart_and_payment_results(cart_result, payment_result)
            if cart_result is not None
            else payment_result
        )
        payment_changed_during_review = bool(
            state.order_confirmation_pending
            and payment_preference is not None
            and payment_preference != state.payment_method_preference
        )
        refreshed_payment_state = evolve_commerce_state(payment_state, combined_result)
        if (
            payment_changed_during_review
            and refreshed_payment_state.selected_payment_option is not None
        ):
            review_result = await prepare_order(
                state=refreshed_payment_state,
                execute=execute_tool,
            )
            combined_result = _combine_checkout_and_followup_results(
                combined_result, review_result,
            )
        if interpretation.checkout_channel_preference is not None:
            channel_result = select_checkout_channel(
                payment_state,
                interpretation.checkout_channel_preference,
            )
            combined_result = _combine_checkout_channel_result(
                combined_result,
                channel_result,
            )
        final = await _sales_response_with_openai(
            message,
            plan,
            combined_result,
            interpretation,
            evolve_commerce_state(payment_state, combined_result),
        )
        if final:
            return final
        return _mark_sales_result(
            combined_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if payment_result.safety_reason == "payment_options_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(combined_result.response_metadata.get("used_tray")),
            fallback_reason=payment_result.safety_reason,
        )
    if purchase_action == "inspect_cart":
        cart_result = await inspect_current_cart(state=state, execute=execute_tool)
        final = await _sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if cart_result.safety_reason == "cart_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(cart_result.response_metadata.get("used_tray")),
            fallback_reason=cart_result.safety_reason,
        )
    if purchase_action in {"show_cart_link", "checkout_question"}:
        cart_result = current_cart_reply(
            state,
            checkout_question=purchase_action == "checkout_question",
        )
        final = await _sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        print("[sales.responder]", {
            "source": "openai" if final else "deterministic_fallback",
        })
        if final:
            return final
        return _mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="sales_responder_unavailable",
        )
    if purchase_action == "create_cart" and unresolved_purchase_items:
        log_purchase_progress(
            "product_resolution",
            "blocked",
            "purchase_item_unresolved",
        )
        return _mark_sales_result(
            AgentResult(
                reply_text="Encontrei mais de uma possibilidade. Confirme quais itens da lista devem entrar no carrinho.",
                intent="commerce",
                handoff_required=False,
                safety_reason="ambiguous_purchase_item",
                commercial_data={
                    "products": unresolved_candidates or [
                        item.model_dump(mode="json")
                        for item in state.last_presented_products
                    ],
                    "cart": {"status": "item_clarification_required"},
                },
                response_metadata={"presented_products": bool(unresolved_candidates)},
            ),
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="purchase_item_unresolved",
        )
    if purchase_action == "create_cart" and (purchase_requests or resolved_product is not None):
        if purchase_requests:
            cart_result = await create_cart_items_checkout(
                item_requests=purchase_requests,
                state=state,
                execute=execute_tool,
            )
        else:
            cart_result = await create_cart_checkout(
                interpretation=interpretation,
                product_reference=resolved_product,
                state=state,
                execute=execute_tool,
            )
        if (
            cart_result.safety_reason is None
            and interpretation.checkout_channel_preference is not None
        ):
            checkout_state = evolve_commerce_state(state, cart_result)
            channel_result = select_checkout_channel(
                checkout_state,
                interpretation.checkout_channel_preference,
            )
            cart_result = _combine_checkout_channel_result(
                cart_result,
                channel_result,
            )
        final = await _sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        print("[sales.responder]", {
            "source": "openai" if final else "deterministic_fallback",
        })
        if final:
            return final
        return _mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if cart_result.safety_reason == "cart_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(cart_result.response_metadata.get("used_tray", True)),
            fallback_reason=cart_result.safety_reason or "sales_responder_unavailable",
        )
    discovery_state = (
        _discovery_state(
            interpretation,
            recent_turns,
            message_text=message.text,
            commerce_state=state,
        )
        if interpretation
        else None
    )
    if discovery_state and discovery_state["force_retrieval"] and plan.get("intent") == "clarification":
        plan = {**plan, "intent": "recommendation"}
    print("[sales.agent] planner", {
        "source": plan.get("_source", "fallback"),
        "action": plan.get("intent"),
        "has_query": bool(plan.get("query")),
        "has_brand": bool((plan.get("filters") or {}).get("brand")),
        "has_model": bool((plan.get("filters") or {}).get("model")),
    })
    if discovery_state:
        print("[sales.discovery]", {
            "clarification_count": discovery_state["clarification_count"],
            "enough_information_to_search": discovery_state["enough_information_to_search"],
            "ready_for_retrieval": discovery_state["ready_for_retrieval"],
            "stop_clarification": discovery_state["stop_clarification"],
            "known_preferences_count": discovery_state["known_preferences_count"],
        })
    vague_query = str(plan.get("query") or "").strip().lower() in {"", "alguma coisa", "algo", "qualquer coisa", "um produto", "uma coisa", "produto"}
    browse_reset = False
    if interpretation is not None:
        try:
            from .sales.dialogue_phase import message_resets_dialogue_to_discovery

            browse_reset = message_resets_dialogue_to_discovery(
                message.text,
                interpretation,
            )
        except Exception:
            browse_reset = False
    if interpretation and discovery_state and not browse_reset:
        from .sales.purchase_selection import blocks_persona_qualification_for_purchase

        if blocks_persona_qualification_for_purchase(interpretation, state):
            # Mid-checkout / shortlist close: never reopen persona or fresh browse.
            discovery_state = {
                **discovery_state,
                "persona_qualification_required": False,
                "force_retrieval": False,
            }
    if interpretation and discovery_state and _needs_clarification_before_retrieval(interpretation, plan, discovery_state):
        return await generate_clarification_reply(
            message=message,
            interpretation=interpretation,
            recent_turns=recent_turns,
            discovery_state=discovery_state,
        )
    if interpretation and discovery_state and vague_query and not discovery_state["force_retrieval"]:
        from .sales.purchase_selection import blocks_persona_qualification_for_purchase

        if not blocks_persona_qualification_for_purchase(interpretation, state):
            return await generate_clarification_reply(
                message=message,
                interpretation=interpretation,
                recent_turns=recent_turns,
                discovery_state=discovery_state,
            )
    if plan.get("intent") == "clarification" or vague_query:
        from .sales.purchase_selection import blocks_persona_qualification_for_purchase

        if blocks_persona_qualification_for_purchase(interpretation, state):
            clarification = str(
                (interpretation.clarification_question if interpretation else None)
                or ""
            ).strip()
            if clarification:
                return _mark_sales_result(
                    AgentResult(
                        reply_text=html.unescape(clarification),
                        intent="commerce",
                        handoff_required=False,
                        safety_reason="purchase_close_hold",
                        response_metadata={"domain": "commerce"},
                    ),
                    interpretation=interpretation,
                    goal=(interpretation.goal if interpretation else "buy"),
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=False,
                    fallback_reason="purchase_close_hold",
                )
            return _mark_sales_result(
                AgentResult(
                    reply_text=(
                        "Qual opção da lista você quer comprar "
                        "(1, 2 ou 3)?"
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="purchase_close_hold",
                    response_metadata={"domain": "commerce"},
                ),
                interpretation=interpretation,
                goal=(interpretation.goal if interpretation else "buy"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="purchase_close_hold",
            )
        clarification = str(plan.get("clarification_question") or "").strip()
        if not clarification and interpretation is not None:
            clarification = (
                _persona_qualification_question(
                    interpretation,
                    discovery_state,
                )
                or str(interpretation.clarification_question or "").strip()
            )
        if not clarification:
            return await generate_clarification_reply(
                message=message,
                interpretation=interpretation
                or SalesInterpretation(
                    domain="commerce",
                    goal="discover",
                    needs_clarification=True,
                ),
                recent_turns=recent_turns,
                discovery_state=discovery_state,
            )
        result = AgentResult(
            reply_text=clarification,
            intent="commerce",
            handoff_required=False,
            safety_reason="commerce_clarification",
        )
        return _mark_sales_result(
            result,
            interpretation=None,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
        )

    action = {
        "product_search": "product_search",
        "recommendation": "product_search",
        "product_comparison": "product_search",
        "price": "product_price",
        "inventory": "product_inventory",
        "coupon": "coupon_search",
    }.get(str(plan.get("intent")))
    if not action:
        return None

    if interpretation is not None and resolved_product is not None:
        tray_result = await _execute_contextual_product_lookup(
            interpretation,
            resolved_product,
        )
    elif interpretation is not None and action == "product_search":
        tray_result = await _execute_compiled_product_retrieval(
            interpretation,
            message_text=message.text,
        )
    else:
        queries = [str(plan.get("query") or "").strip()]
        code_value = re.sub(r"^(?:ean|sku|ref(?:er[êe]ncia)?)\s+", "", queries[0], flags=re.IGNORECASE)
        code_query = bool(re.fullmatch(r"[A-Za-z0-9._/-]+", code_value)) and any(char.isdigit() for char in code_value)
        subject = plan.get("subject") or {}
        if action == "product_search" and not code_query:
            model = str(subject.get("model") or "").strip()
            brand = str(subject.get("brand") or "").strip()
            if model:
                queries.append(model)
            if brand:
                queries.append(brand)
        queries = list(dict.fromkeys(query for query in queries if query or action == "coupon_search"))
        tray_result = None
        last_raw_result = None
        for attempt, query in enumerate(queries[:3], start=1):
            attempt_plan = {**plan, "query": query, "subject": {**(plan.get("subject") or {}), "query": query}}
            print("[sales.agent] tray_request", {"capability": action, "attempt": attempt, "strategy": "initial" if attempt == 1 else "progressive"})
            raw_result = await handle_commerce_message(
                message,
                facts,
                customer_context,
                action=action,
                query=query,
            )
            last_raw_result = raw_result
            print("[sales.agent] tray_result", {"ok": raw_result is not None and raw_result.safety_reason != "tray_adapter_unavailable", "results_count": len((raw_result.commercial_data or {}).get("products", [])) if raw_result else 0})
            tray_result = _ranked_result(raw_result, attempt_plan) if raw_result else None
            if tray_result:
                print("[sales.agent] ranking", {"input_count": len((raw_result.commercial_data or {}).get("products", [])), "output_count": len((tray_result.commercial_data or {}).get("products", []))})
                break
            if raw_result and raw_result.safety_reason == "tray_adapter_unavailable":
                tray_result = raw_result
                break
            if raw_result and raw_result.safety_reason not in {"product_not_found", "ambiguous_product"}:
                tray_result = raw_result
                break
        if tray_result is None:
            tray_result = last_raw_result
    if tray_result is None:
        return None
    if interpretation is not None:
        phase_hint: dict[str, Any] = {}
        try:
            from .sales.dialogue_phase import metadata_dialogue_phase_hint

            phase_hint = metadata_dialogue_phase_hint(
                interpretation=interpretation,
                message_text=message.text,
                presented_products=bool(
                    (tray_result.response_metadata or {}).get("presented_products")
                ),
            )
        except Exception:
            phase_hint = {}
        tray_result.response_metadata.update({
            "active_topic": interpretation.active_topic,
            "purchase_stage": interpretation.purchase_stage,
            "active_preferences": interpretation.preferences.model_dump(
                mode="json",
                exclude_none=True,
            ),
            **phase_hint,
        })
        if resolved_product is not None:
            tray_result.response_metadata["active_product"] = resolved_product.model_dump(mode="json")
    if (
        plan.get("intent") in {"purchase_intent", "recommendation", "clarification"}
        and tray_result.safety_reason == "product_not_found"
        and not (discovery_state and discovery_state["force_retrieval"])
    ):
        if interpretation:
            return await generate_clarification_reply(
                message=message,
                interpretation=interpretation,
                recent_turns=recent_turns,
                context_note="A busca atual não trouxe candidatos confiáveis; peça um critério diferente sem afirmar que o produto não existe.",
                used_tray=True,
                discovery_state=discovery_state,
            )
    final = await _sales_response_with_openai(
        message,
        plan,
        tray_result,
        interpretation,
        evolve_commerce_state(state, tray_result),
    )
    print("[sales.agent] responder", {"source": "openai" if final else "deterministic_fallback"})
    if final:
        return final
    if interpretation is not None and _comparison_needs_qualification(interpretation):
        return await generate_clarification_reply(
            message=message,
            interpretation=interpretation,
            recent_turns=recent_turns,
            discovery_state=discovery_state,
        )
    technical_failure = tray_result.safety_reason in {
        "tray_adapter_unavailable",
        "product_match_failed",
    }
    response_source = "technical_fallback" if technical_failure else "deterministic_fallback"
    return _mark_sales_result(
        tray_result,
        interpretation=interpretation,
        goal=plan.get("goal"),
        response_source=response_source,
        used_openai_responder=False,
        used_tray=True,
        fallback_reason=(
            tray_result.safety_reason
            if response_source == "technical_fallback"
            else "sales_responder_unavailable"
        ),
    )
