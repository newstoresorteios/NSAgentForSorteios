# Smoke test de integração

Script CLI que valida os três serviços integrados em produção/staging **sem gravar segredos no repositório**.

## Checks

| Prioridade | Serviço | Endpoint | Critério |
|------------|---------|----------|----------|
| P0 | NSAgent | `GET /api/health` | `ok`, `agent_version` e `tray_adaptor_probe.ok` |
| P0 | TRAYadaptor | `GET /health/tray` | `access_valid: true` |
| P0 | Chatbo | `GET /health` | `status: "ok"` |
| Opcional | TRAYadaptor | `GET /health` | `status: "ok"` |
| Opcional | Local | env | `TRAY_ADAPTER_URL` + `TRAY_ADAPTER_TOKEN` definidos |
| Opcional | Supabase | `GET /rest/v1/` | reachability (HTTP 200/400/401) |

## Variáveis de ambiente

Defina no shell ou em `.env` / `.env.local` (carregados automaticamente):

```bash
NSAGENT_BASE_URL=https://ns-agent-for-sorteios.vercel.app
TRAY_ADAPTER_URL=https://seu-tray-adaptor.onrender.com
TRAY_ADAPTER_TOKEN=seu-token-interno
CHATBO_BASE_URL=https://seu-chatbo.onrender.com
# Opcional — ping de conectividade
SUPABASE_URL=https://xxxx.supabase.co
# Opcional — timeout por request (segundos)
SMOKE_TEST_TIMEOUT_S=10
```

**Alinhamento TRAY:** o mesmo par `TRAY_ADAPTER_URL` + `TRAY_ADAPTER_TOKEN` deve estar configurado no NSAgent (env) e no TRAYadaptor (`TRAY_ADAPTER_TOKEN` no servidor). No Chatbo a integração Tray é por workspace (banco), não por env global — use o painel de integrações para conferir URL/token equivalentes.

## Uso

```bash
# Na raiz do repo, com dependências instaladas
pip install -r requirements.txt

# Exporte as URLs (ou use .env.local)
export NSAGENT_BASE_URL=...
export TRAY_ADAPTER_URL=...
export TRAY_ADAPTER_TOKEN=...
export CHATBO_BASE_URL=...

python scripts/integration_smoke_test.py
```

Saída esperada (exemplo):

```text
=== Smoke test de integração (NSAgent + TRAY + Chatbo) ===

Check                        Crítico  Resultado Detalhe
-------------------------------------------------------
NSAgent /api/health          sim      PASS     HTTP 200; agent_version='v61'; tray_adaptor_probe.ok=True
TRAYadaptor /health/tray     sim      PASS     HTTP 200; access_valid=True; store_id='...'
...
OK: todos os checks passaram.
```

- **Exit code 0** — todos os checks **críticos** passaram.
- **Exit code 1** — pelo menos um check crítico falhou.

Tokens nunca aparecem inteiros na saída (`TRAY_ADAPTER_TOKEN` é mascarado no check de env).

## CI (sem URLs live)

```bash
pytest tests/test_integration_smoke_script.py
```

Os testes usam `httpx.MockTransport` — não precisam de credenciais de produção.

## Chatbo

Documentação espelhada em `Chatbo-backendAgent/scripts/integration_smoke_checklist.md` (checklist de env e referência a este script).
