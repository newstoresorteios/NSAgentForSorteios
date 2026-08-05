# Instagram Story ↔ product recognition (v6)

## Diagnóstico do payload

| Item | Situação atual |
|------|----------------|
| Provedor | **Brevo Conversations** (omnichannel). Não há webhook Meta Graph direto no código. |
| Entrada | `POST /api/webhooks/brevo/conversations` (+ alias WhatsApp) → `parse_brevo_conversations_payload` |
| Story hoje | **Não havia** parsing de `reply_to.story` / `story_mention` antes desta entrega |
| Campos usados | `reply_to` / `replyTo` / `story` / `referral` / `attachments[].type=story_mention` / soft `source` |
| Mídia | URL em `story.url` ou `attachments[].payload.url` (frequentemente assinada — **query é removida na persistência/log**) |
| Limitações | Brevo pode omitir ou transformar campos Meta; Stories expirados perdem URL; vídeo exige decoder (fallback: thumbnail ou pedir print) |
| Diagnostics | `INSTAGRAM_STORY_PAYLOAD_DIAGNOSTICS=true` registra só nomes/tipos de campos (sem tokens/URLs assinadas) |

## Fluxo

```text
Webhook Brevo → IncomingMessage.instagram_story
→ (flag + rollout) resolve_story_product_question
→ associação DB → mídia segura → visão (1x) → candidatos reais
→ Tray revalidate → resposta / esclarecimento
```

Prioridade: publication_metadata → manual → hash → EAN/SKU/ref → índice/Tray → esclarecimento.

Visão **nunca** é autoridade de preço/estoque.

## Migration

- Arquivo: `sql/019_instagram_story_products.sql`
- Ordem: após `018`
- Aplicar no Postgres da app (mesmo processo das SQL anteriores)
- Rollback: `DROP TABLE public.instagram_story_products;`
- `ensure_tables()` também cria a tabela quando `AUTO_CREATE_TABLES=true`

## Rollout recomendado

1. Aplicar migration 019  
2. `INSTAGRAM_STORY_PAYLOAD_DIAGNOSTICS=true` em staging (curto)  
3. Confirmar campos reais no log sanitizado  
4. `INSTAGRAM_STORY_RECOGNITION_ENABLED=true` + `ROLLOUT_MODE=shadow`  
5. Canary 5% → 25% → full  
6. Preferir `POST /api/admin/instagram/stories/link-product` na publicação  

## Admin API (token)

- `GET /api/admin/instagram/stories`
- `GET /api/admin/instagram/stories/{id}`
- `POST /api/admin/instagram/stories/link-product`
- `POST /api/admin/instagram/stories/{id}/confirm`
- `POST /api/admin/instagram/stories/{id}/unlink`
- `POST /api/admin/instagram/stories/{id}/reprocess`

Body de link **não** aceita preço/estoque do cliente.

## Vídeo

`extract_video_frames_best_effort` está reservado. Sem decoder no deploy, usa thumbnail ou pede print. Não adicionar OpenCV sem validar tamanho Render/Vercel.

## Modelos OpenAI

Não alterados (`OPENAI_MODEL` / `MAIN` / `FAST`).
