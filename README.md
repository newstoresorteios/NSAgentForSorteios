# NewStoreAgent — Python Webhook para WhatsApp/Brevo

Projeto Python/FastAPI preparado para Vercel. Ele recebe webhooks inbound da Brevo, registra auditoria, chama OpenAI pelo SDK oficial e retorna uma resposta segura.

> Este projeto não inclui credenciais reais. Configure tudo em Environment Variables na Vercel.

## Stack

- Python + FastAPI
- Vercel Python Runtime
- OpenAI Python SDK + Responses API
- PostgreSQL/Supabase via `psycopg`
- Brevo inbound webhook

## Arquivos principais

```txt
api/index.py                         # FastAPI app para Vercel
app/webhook_parser.py                # Parser defensivo do payload Brevo
app/openai_agent.py                  # Chamada OpenAI + instruções do agente
app/brevo_client.py                  # Adapter outbound configurável/dry-run
app/db.py                            # Auditoria em Postgres
app/repository.py                    # Lookup mínimo por telefone
sql/001_ai_agent_audit.sql           # Tabelas de auditoria
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
