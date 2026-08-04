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
OPENAI_API_MODE=chat_completions   # default — sem mudança de comportamento
# OPENAI_API_MODE=responses        # 100% Responses
# OPENAI_API_MODE=shadow           # Chat em produção + sample Responses
# OPENAI_API_MODE=canary           # % sticky Responses + fallback Chat
OPENAI_RESPONSES_TRAFFIC_PERCENT=0.05
OPENAI_RESPONSES_FALLBACK_TO_CHAT=true
OPENAI_CANARY_STICKY_ROUTING=true
OPENAI_STORE_RESPONSES=false
OPENAI_USE_PREVIOUS_RESPONSE_ID=false
```

- **Fase 1–3:** gateway / structured / texto+tools (pronto).
- **Fase 4–6:** persona + memória audit + auto-apply allowlist (pronto; flags off).
- **Fase 7:** canary % Responses (pronto; `OPENAI_API_MODE=canary` + percent).
- **Fase 8:** Chat Completions rebaixado a rollback/fallback (pronto; código mantido).
Default de produção: `OPENAI_API_MODE=chat_completions` (rollback seguro).
Após canary verde: `canary` → `responses` + `OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED=false`.

### Chat Completions (Fase 8 — rollback only)

Mantido **apenas** para:
- `OPENAI_API_MODE=chat_completions` (emergência; loga depreciação);
- shadow (primary Chat + sample Responses);
- fallback quando Responses falha (`OPENAI_RESPONSES_FALLBACK_TO_CHAT=true`).

Com `OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED=false`, o mode `chat_completions`
redireciona para Responses (+ fallback Chat). Tool loops **nunca** fazem fallback.

### Persona versionada (Fase 4)

```txt
sql/009_ai_agent_persona.sql         # ai_agent_persona_versions + ai_prompt_compilations
persona NS.txt                       # conteúdo exato da v1 (seed)
scripts/seed_newstore_persona.py     # seed idempotente
AGENT_DB_PERSONA_ENABLED=false       # liga uso da persona ativa do banco
```

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

### Memória (Fase 5 — auditoria)

```txt
sql/010_ai_memory_proposals.sql
AGENT_MEMORY_PROPOSALS_ENABLED=false          # envelope estruturado + persistência
AGENT_MEMORY_AUTO_APPLY_ENABLED=false         # nunca aplicar em produção ainda
AGENT_INSTRUCTION_EXTENSION_PROPOSALS_ENABLED=false
```

Com propostas ligadas, o responder comercial usa `AgentTurnEnvelope` (reply + proposals)
na mesma chamada; o backend valida (`memory_policy`) e grava em `ai_memory_proposals`
sem alterar a persona nem aplicar memória de contato.

Auto-apply (Fase 6) exige **ambos**:
`AGENT_MEMORY_AUTO_APPLY_ENABLED=true` **e** `AGENT_MEMORY_AUTO_APPLY_SENDER_ALLOWLIST`
(lista de `sender_key` ou `*`). Thresholds: confidence ≥ 0.85, importance ≥ 0.70,
kinds allowlisted, evidência explícita. Extensões tenant nunca auto-aplicam.

## Arquivos principais

```txt
api/index.py                         # FastAPI app para Vercel
app/webhook_parser.py                # Parser defensivo do payload Brevo
app/openai_agent.py                  # Chamada OpenAI + instruções do agente
app/openai_gateway.py                # Gateway Chat Completions / Responses / shadow
app/openai_client.py                 # Cliente AsyncOpenAI compartilhado
app/prompt_compiler.py               # Compila instructions (persona + overlays)
app/persona_repository.py            # CRUD versionado da persona
app/persona_admin_api.py             # Admin API de personas
app/brevo_client.py                  # Adapter outbound configurável/dry-run
app/db.py                            # Auditoria em Postgres
app/repository.py                    # Lookup mínimo por telefone
sql/001_ai_agent_audit.sql           # Tabelas de auditoria
sql/009_ai_agent_persona.sql         # Persona versionada + audit de prompt
.env.example                         # Variáveis sem segredos reais
vercel.json                          # Config Vercel
```

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
