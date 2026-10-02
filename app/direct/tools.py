"""Small validated read-only tool surface; no generic execute_tool dispatcher."""
from __future__ import annotations

import asyncio
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.direct.knowledge import search_knowledge


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Search(Arguments):
    query: str = Field(min_length=1, max_length=160)
    brand: str | None = Field(default=None, max_length=100)
    max_price: float | None = Field(default=None, ge=0)
    ready_stock: bool = False


class Product(Arguments):
    product_id: str = Field(pattern=r"^[0-9]{1,18}$")


class Knowledge(Arguments):
    query: str = Field(min_length=1, max_length=300)


class Handoff(Arguments):
    reason: str = Field(min_length=1, max_length=300)


class ReadyDelivery(Knowledge):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)
    snapshot_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


class ProductImage(Arguments):
    product_url: str = Field(min_length=1, max_length=2000)


DEFINITIONS = {
    "search_ready_delivery": (ReadyDelivery, "Consultar pronta entrega em www.newstorerj.com pelo adaptador. Busca ampla: query='pronta entrega', offset=0. Para mais modelos, mantenha query e use next_offset. total é o total encontrado; returned é só esta página. Não acrescente preferências antigas. Listagem pública não confirma estoque físico ou preço."),
    "prepare_product_image": (ProductImage, "Anexar à resposta a foto de um produto já retornado pelas consultas. Informe seu product_url exato; nunca invente URLs. Se a foto não estiver disponível, consulte o modelo na mesma fonte antes de tentar novamente."),
    "search_products": (Search, "Buscar produtos reais por palavras do catálogo; faça buscas curtas. Valores em BRL. ready_stock só para pronta entrega."),
    "get_product": (Product, "Consultar detalhes atuais do produto e seu link oficial; use IDs retornados pela busca."),
    "check_inventory": (Product, "Confirmar disponibilidade atual; não confundir estoque, prazo de postagem e chegada."),
    "search_knowledge": (Knowledge, "Consultar documentos publicados da loja, políticas, garantia e perguntas frequentes."),
    "request_human": (Handoff, "Pedir encaminhamento humano. Backend valida consentimento explícito; não efetua operações comerciais."),
}


def tool_schemas():
    tools = []
    for name, (model, description) in DEFINITIONS.items():
        schema = model.model_json_schema()
        # Responses strict requires all properties, nullable for optional fields.
        schema["required"] = list(schema["properties"])
        for value in schema["properties"].values():
            value.pop("default", None)
        tools.append({"type": "function", "name": name, "description": description,
                      "parameters": schema, "strict": True})
    return tools


class DirectTools:
    def __init__(self, *, incoming, history, documents, adapter=None, products=None):
        self.incoming, self.history, self.documents = incoming, history, documents
        self.adapter = adapter
        self.handoff = None
        self.calls = []
        self.products = {p['url']: p for p in (products or []) if isinstance(p, dict) and p.get('url')}
        self.outbound_image_url = None
        self.catalog_searches = []

    async def execute(self, name: str, raw: str) -> dict:
        if name not in DEFINITIONS:
            return {"ok": False, "error": "tool_not_allowed"}
        try:
            args = DEFINITIONS[name][0].model_validate_json(raw)
        except (ValidationError, ValueError):
            return {"ok": False, "error": "invalid_tool_arguments"}
        self.calls.append(name)
        if name == "prepare_product_image":
            product = self.products.get(args.product_url) or {}
            from urllib.parse import urlparse
            url = product.get('image_url') or product.get('primary_image_url') or ''
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.tcdn.com.br'):
                return {"ok": False, "error": "verified_product_image_unavailable"}
            self.outbound_image_url = url
            return {"ok": True, "image_attached_to_reply": True, "product": product.get('name')}
        if name == "search_knowledge":
            return search_knowledge(self.documents, args.query)
        if name == "request_human":
            from app.ops.handoff_consent import consent_reason
            confirmed = consent_reason(self.incoming, self.history)
            if not confirmed:
                return {"ok": False, "error": "explicit_customer_consent_required"}
            self.handoff = {"required": True, "confirmed": True, "offer": False,
                            "consent_reason": confirmed, "reason": "customer_requested_human",
                            "provider_action": "mark_for_human", "summary": args.reason}
            return {"ok": True, "handoff_requested": True}
        from app.tray.tray_adapter_client import TrayAdapterClient, TrayAdapterError
        adapter = self.adapter or TrayAdapterClient()
        try:
            async with asyncio.timeout(20):
                if name == "search_products":
                    if args.ready_stock:
                        return {"ok": False, "error": "use_ready_delivery_source",
                                "instruction": "Use search_ready_delivery para pronta entrega; não use available_in_store."}
                    result = await adapter.search_products(
                        name=args.query, brand=args.brand, available=True, limit=5,
                        available_in_store=True if args.ready_stock else None,
                        current_price_range=f"0,{args.max_price:g}" if args.max_price is not None else None)
                elif name == "search_ready_delivery":
                    result = await adapter.search_ready_delivery(args.query, offset=args.offset, limit=args.limit,
                                                               snapshot_id=args.snapshot_id)
                elif name == "get_product":
                    result = await adapter.get_product(args.product_id)
                else:
                    result = await adapter.get_product_stock(args.product_id)
            # Only normalized public product fields leave the backend.
            data = public_product_data(result)
            if name == 'search_ready_delivery' and isinstance(data, dict):
                self.catalog_searches.append({k: data.get(k) for k in
                    ('query', 'total', 'returned', 'offset', 'limit', 'has_more', 'next_offset', 'snapshot_id')})
            if isinstance(data, dict):
                products = data.get('products') or [data.get('product') or data]
                for product in products:
                    if isinstance(product, dict) and product.get('url'):
                        self.products[product['url']] = product
                self.products = dict(list(self.products.items())[-60:])
            return {"ok": True, "source": "tray_adapter", "data": data}
        except (TrayAdapterError, TimeoutError) as exc:
            if name == 'search_ready_delivery' and getattr(exc, 'status_code', None) == 409:
                return {"ok": False, "error": "catalog_snapshot_expired",
                        "instruction": "A lista expirou. Consulte novamente com snapshot_id=null, offset=0; avise que atualizou a lista e evite repetir modelos já apresentados."}
            return {"ok": False, "source": "tray_adapter", "error": "commerce_unavailable",
                    "instruction": "Não confirme produto, preço, estoque ou prazo sem resultado válido."}


PUBLIC_FIELDS = frozenset({"id", "product_id", "name", "reference", "ean", "brand", "model",
    "image_url", "query", "returned", "offset", "has_more", "next_offset", "snapshot_id",
    "success", "source", "checkedAt", "complete", "requiresModel", "evidenceType", "stockConfirmed", "listedAvailable",
    "description", "category", "category_name", "category_id", "mechanism", "case_size",
    "water_resistance_m", "water_resistance", "color", "style", "material", "gender",
    "price", "promotional_price", "current_price", "stock", "available", "availability",
    "available_in_store", "available_for_purchase", "upon_request", "when_stock_runs_out",
    "order_days_availability", "lead_time_days", "has_variation", "url", "product_url",
    "primary_image_url", "products", "product", "data", "paging", "page", "total", "limit"})


def public_product_data(value):
    if isinstance(value, dict):
        return {k: public_product_data(v) for k, v in value.items() if k in PUBLIC_FIELDS}
    if isinstance(value, list):
        return [public_product_data(v) for v in value]
    return value[:5000] if isinstance(value, str) else value
