# NewStoreAgent — Python Webhook para WhatsApp/Brevo

Projeto Python/FastAPI preparado para Vercel. Ele recebe webhooks inbound da Brevo, registra auditoria, chama OpenAI pelo SDK oficial e retorna uma resposta segura.

> Este projeto não inclui credenciais reais. Configure tudo em Environment Variables na Vercel.

## Stack

- Python + FastAPI
- Vercel Python Runtime
- OpenAI Python SDK (`openai==2.7.2`) — Chat Completions (produção) + gateway Responses
- PostgreSQL/Supabase via `psycopg`
- Brevo inbound webhook

## OpenAI API mode (migração)

```txt
OPENAI_API_MODE=chat_completions   # default seguro / rollback
# OPENAI_API_MODE=canary           # % sticky Responses + fallback Chat (texto/structured)
# OPENAI_API_MODE=responses        # 100% Responses (+ fallback Chat se habilitado)
# OPENAI_API_MODE=shadow           # Chat em produção + sample Responses
OPENAI_RESPONSES_TRAFFIC_PERCENT=0.10
OPENAI_RESPONSES_FALLBACK_TO_CHAT=true
OPENAI_CANARY_STICKY_ROUTING=true
OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED=true
OPENAI_STORE_RESPONSES=false
OPENAI_USE_PREVIOUS_RESPONSE_ID=false
```

**Rollout sugerido**

1. `OPENAI_API_MODE=canary` + `OPENAI_RESPONSES_TRAFFIC_PERCENT=0.05` (sticky por conversa)
2. Subir para `0.10` após métricas verdes
3. `OPENAI_API_MODE=responses` + `OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED=false`

Tool loops **nunca** fazem fallback para Chat (evita mutação dupla Tray/carrinho/pedido).
Texto/structured podem cair para Chat quando `OPENAI_RESPONSES_FALLBACK_TO_CHAT=true`.

### Persona versionada (produção)

```txt
sql/009_ai_agent_persona.sql         # ai_agent_persona_versions + ai_prompt_compilations
persona NS.txt                       # conteúdo exato da v1 (seed)
scripts/seed_newstore_persona.py     # seed idempotente
AGENT_DB_PERSONA_ENABLED=true        # tom/identidade do banco; fallback = contrato em código
```

Sem persona ativa/ falha de DB → usa contrato operacional em código + `<fixed_safety_policy>`.
Persona **não** pode embutir preço/estoque/link de checkout voláteis.

Admin (Bearer `ADMIN_API_TOKEN`):

```txt
GET/POST /api/admin/agents/{tenant_id}/personas
GET      /api/admin/agents/{tenant_id}/personas/active
POST     /api/admin/agents/{tenant_id}/personas/{id}/activate|archive|rollback
GET      /api/admin/agents/{tenant_id}/prompt-preview
GET/POST /api/admin/agents/{tenant_id}/instruction-extensions
POST     /api/admin/agents/{tenant_id}/instruction-extensions/{id}/approve|reject
GET/POST /api/admin/agents/{tenant_id}/contacts/{sender_key}/memories
DELETE   /api/admin/agents/{tenant_id}/contacts/{sender_key}/memories/{memory_key}
```

### Memória (propostas + inject)

```txt
sql/010_ai_memory_proposals.sql
AGENT_MEMORY_PROPOSALS_ENABLED=true           # envelope estruturado + persistência
AGENT_CONTACT_MEMORY_IN_PROMPT_ENABLED=true   # injeta memórias ativas no prompt
AGENT_MEMORY_AUTO_APPLY_ENABLED=false         # manter off até allowlist
AGENT_CONVERSATION_SUMMARY_ENABLED=false      # critérios/async; não a cada turno
AGENT_INSTRUCTION_EXTENSION_PROPOSALS_ENABLED=false
AGENT_LEARNING_AUTO_PROMOTE=true              # continuous ACE loop
AGENT_LEARNING_AUTO_ACTIVATE=true             # canary + KPI rollback; kill-switch=false
```

Com propostas ligadas, o responder comercial usa `AgentTurnEnvelope` (reply + proposals)
na mesma chamada; o backend valida (`memory_policy`) e grava em `ai_memory_proposals`.
Summary só aplica delta com progresso real (compromisso, correção, falha, meta nova…).

Auto-apply exige **ambos**:
`AGENT_MEMORY_AUTO_APPLY_ENABLED=true` **e** `AGENT_MEMORY_AUTO_APPLY_SENDER_ALLOWLIST`
(lista de `sender_key` ou `*`). Thresholds: confidence ≥ 0.85, importance ≥ 0.70,
kinds allowlisted, evidência explícita. Extensões propostas no turno comercial
ficam `pending_review` até o admin. O loop de learning contínuo auto-ativa
deltas de prompt com constituição + canary; kill-switch:
`AGENT_LEARNING_AUTO_PROMOTE=false` e `AGENT_LEARNING_AUTO_ACTIVATE=false`.

## Arquivos principais

```txt
api/index.py                         # Entrypoint fino para Vercel
app/http/                            # Factory FastAPI, webhooks, health, admin e crons
app/channels/                        # Brevo, Meta, áudio e normalização de entrada
app/agents/                          # Porta de entrada e orquestração por domínio
app/sales/                           # Interpretação, contrato do turno e fluxo comercial
app/catalog/                         # Índice, retrieval, ranking, mídia e visão
app/commerce/                        # Carrinho, pedido, checkout e PIX
app/llm/                             # Gateway OpenAI, compiler, routing e apresentação
app/memory/                          # Memória de contato, resumo e retomada de contexto
app/verify/                          # Autoridade factual, council, judge e compliance
app/ingress/                         # Inbox/outbox, leases e workers
app/ops/                             # Runtime, tracing, locks, KPIs e rollout
tests/evals/                         # Replays e evals offline
scripts/validate_project_config.py   # Valida Settings contra .env.example
.env.example                         # Variáveis sem segredos reais
vercel.json                          # Rotas e agendas Vercel
```

O retrieval trabalha com pools internos maiores (20 candidatos por padrão e
descoberta paginada), mas apresenta normalmente três opções ao cliente. Em busca
sem marca travada, a seleção final prioriza marcas distintas. Quando só existe
uma marca dentro do orçamento e disponibilidade atuais, a resposta declara esse
limite e pede autorização antes de ampliar a faixa.

Replay offline (agente real + fakes; score não copia `expected`):

```bash
pytest -m offline_eval
```

Detalhes: `docs/agent_generative_migration_etapa11.md`.

Canary progressivo / rollback (Etapa 12): `AGENT_ROLLOUT_PROFILE=canary_5|…|full` ou `AGENT_EMERGENCY_ROLLBACK=true`. Status: `GET /api/health` → `rollout` ou `GET /api/admin/rollout`. Doc: `docs/agent_generative_migration_etapa12.md`.

## Segurança obrigatória

As chaves reais devem ficar somente na Vercel:

```txt
OPENAI_API_KEY
DATABASE_URL
BREVO_API_KEY
BREVO_WEBHOOK_SECRET
ADMIN_API_TOKEN
```

Não suba `.env` para GitHub.

## Deploy na Vercel

1. Suba este projeto para um repositório.
2. Na Vercel, importe o repositório.
3. Configure as Environment Variables usando `.env.example` como referência.
4. Rode a SQL `sql/001_ai_agent_audit.sql` no banco, ou defina `AUTO_CREATE_TABLES=true` temporariamente.
5. Configure na Brevo o webhook apontando para:

```txt
https://SEU-DOMINIO.vercel.app/api/webhooks/brevo/whatsapp
```

6. Configure na Brevo um header customizado:

```txt
X-Webhook-Token: mesmo_valor_de_BREVO_WEBHOOK_SECRET
```

Antes de promover produção, exija CI verde no SHA final e confira que
`/api/health` apresenta o mesmo `deployment_sha`. Depois do deploy, valide uma
mensagem canário e confirme `_agent_metadata`, `_agent_runtime`, compilação de
prompt e recibo de envio no banco. O workflow `Attendance Learning` deve concluir
com sucesso e avançar seu cursor.

O agente envia `X-Request-ID` ao TRAYadaptor. Use o mesmo `trace_id` nos logs da
Vercel e do Render; o `Rndr-Id` devolvido pelo Render é incorporado ao runtime do
turno.

## Teste local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./scripts/local_dev.sh
```

Health check:

```bash
curl http://localhost:8000/api/health
```

Smoke test cross-project (NSAgent + TRAYadaptor + Chatbo): `python scripts/integration_smoke_test.py` — ver `docs/integration_smoke_test.md`.

Teste do agente:

```bash
curl -X POST http://localhost:8000/api/test/agent \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Olá, preciso de atendimento", "phone":"554399999999", "name":"Teste"}'
```

## Dry-run

Por padrão:

```txt
DRY_RUN=true
BREVO_REPLY_MODE=dry_run
```

Assim o webhook recebe, registra, chama o agente e simula o envio sem mandar mensagem real.

Só desative depois de validar o endpoint outbound correto da Brevo para sua conta:

```txt
DRY_RUN=false
BREVO_REPLY_MODE=brevo
BREVO_SEND_URL=https://...
```

## Observação sobre o agente criado no painel OpenAI

O nome `NewStoreAgent` foi usado como identidade/instrução do agente. Para usar um agente/assistant específico criado no painel, normalmente você precisa do identificador do recurso, não apenas do nome. Este projeto usa a Responses API com instruções equivalentes.

## Limites intencionais

Este boilerplate não implementa ações sensíveis, alteração de dados do cliente, campanhas, disparos ou consultas reguladas. Ele foi feito para atendimento seguro, auditoria e handoff.
