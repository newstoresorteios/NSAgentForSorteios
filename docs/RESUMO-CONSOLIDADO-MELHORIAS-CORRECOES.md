# Resumo consolidado de melhorias e correções

Atualizado em 23/09/2026. Este é o documento canônico para o estado técnico do NSAgent, sua integração com o TRAYadaptor e o histórico de correções. Os 39 relatórios históricos substituídos por este resumo estão preservados em `docs/historical-reports-2026-09-23.zip`; documentos operacionais e políticas de negócio continuam separados porque são fontes vivas, não relatórios de auditoria.

## Parecer atual

O desenho principal está correto: o NSAgent não acessa a API administrativa da Tray nem recebe credenciais OAuth. Toda operação comercial passa por rotas autenticadas `/internal/*` do TRAYadaptor. O adaptador centraliza OAuth, renovação, rate limit, normalização, idempotência e reconciliação de mutações.

O NSAgent mantém um agente focado, com ferramentas de leitura limitadas no tool loop e mutações comerciais em fluxos determinísticos. Esse desenho é compatível com a orientação atual da OpenAI para definir primeiro um agente especializado, manter validação junto às ferramentas com efeitos colaterais e avaliar decisões e chamadas de ferramentas por traces/datasets. Migrar para Agents SDK pode ser uma evolução, mas não é requisito para corrigir a arquitetura existente.

O estado local consolidado está aprovado pelas suítes completas:

| Projeto | Resultado em 23/09/2026 |
| --- | --- |
| TRAYadaptor | 239 testes aprovados |
| NSAgentForSorteios | 2.361 aprovados, 7 pulados, 8 avisos de depreciação do adaptador `sqlite3` |
| Qualidade dos arquivos alterados | `ruff` aprovado e `git diff --check` sem erros |

Os testes pulados são cenários opt-in/externos e não falhas. Esta rodada não executou uma compra real, uma chamada autenticada contra a loja Tray nem um deploy de produção; portanto, a confirmação é de código, contrato e regressão local. O smoke autenticado e a observação pós-deploy continuam sendo gates operacionais.

## Contrato NSAgent ↔ TRAYadaptor

- O NSAgent usa somente `TRAY_ADAPTER_URL` e `TRAY_ADAPTER_TOKEN`.
- Tokens Tray, `consumer_key`, `consumer_secret`, `code` e `api_address` permanecem no adaptador.
- Todas as rotas consumidas pelo cliente do NSAgent existem no FastAPI do adaptador e são cobertas por testes de método, caminho, parâmetros, autenticação e erros.
- GETs transitórios podem ter uma repetição limitada; POST, PUT e DELETE não recebem retry cego.
- Carrinho e pedido reconciliam respostas ambíguas/timeout antes de considerar nova mutação.
- Cancelamento usa `PUT /orders/cancel/:id`, conforme a documentação atual da Tray.
- Webhooks aceitam `application/x-www-form-urlencoded`, registram eventos e permitem consumo incremental por `since_id`.
- Respostas e logs preservam somente diagnósticos seguros; tokens, dados pessoais e URLs sensíveis são removidos ou reduzidos.

## Catálogo, características e variações

A separação canônica é:

- **Produto:** identidade, marca, categoria, descrição, preço-base, disponibilidade e características informativas.
- **Característica (`Property`/`PropertyValues`):** dimensão descritiva e filtrável, como cristal, mecanismo, resistência, material ou gênero.
- **Variação (`Variant`/`Sku`):** opção comprável específica, como cor/tamanho, com `variant_id`, preço e estoque próprios.

Correções consolidadas nesta rodada:

- O normalizador de características do TRAYadaptor agora preserva `has_product` e `PropertyValues` como `values`, em vez de devolver apenas o nome da característica.
- A busca por tokens considera características explícitas do produto (`properties`, mecanismo, caixa, resistência, gênero e material), mas não incorpora opções de variação ao texto do produto. Isso evita afirmar que todo o produto possui uma cor/tamanho pertencente apenas a um SKU.
- O payload reduzido entregue ao NSAgent preserva `mechanism`, `case_size`, `water_resistance_m`, `water_resistance`, `gender` e `attribute_sources`.
- Filtros duros de categoria, disponibilidade, preço e característica continuam ativos quando a pesquisa usa tokens; antes, esse caminho retornava cedo e podia ignorá-los.
- `property_id` e `property_value_id` passaram a integrar o contrato, além de `property_name` e `property_value`.
- O limite de listagem permanece em 50 itens por página. A busca faz probes específicos e só continua a paginação da marca enquanto a página anterior estiver cheia, limitada a 40 páginas e concorrência 2.
- Marca, categoria, orçamento, SKU/EAN/referência e requisitos técnicos permanecem restrições duras; estilo e preferências não exclusivas participam do ranking sem fabricar fatos.
- Resultado de índice local nunca confirma sozinho preço/estoque volátil: os candidatos apresentados são revalidados na Tray quando o fluxo exige fato atual.

A API administrativa da Tray documenta filtros `property_name`, `property_id`, `property_value` e `property_value_id`. O MCP Storefront também oferece `product-properties`, filtro `property` e `variants`; ele é útil para descoberta pública, mas não substitui o adaptador administrativo para pedidos, clientes, pagamentos, estoque autoritativo e webhooks.

## Correções históricas incorporadas no código atual

### Conversa, memória e estado

- Estado comercial recente prevalece sobre memória antiga; nova navegação não herda shortlist, cor ou orçamento obsoletos.
- Troca de produto, seleção numerada, refinamento de variante e retomada de checkout preservam o contexto correto.
- Ausência de preferência não vira veto permanente; pedidos de esquecimento e correções explícitas têm precedência.
- Persona, políticas e configuração são versionadas por workspace, sem usar apelidos do provedor como nome do cliente.

### Catálogo e factualidade

- Filtros compostos são cumulativos e reaplicados após revalidação live.
- Orçamento é teto duro; disponibilidade falsa, falta de preço e incompatibilidade técnica não viram oferta.
- IDs selecionados por modelo são limitados ao conjunto fechado de candidatos reais.
- Preço, estoque, link, pedido e pagamento exigem evidência autorizada; o sistema possui validação final e fallback factual.
- Recomendações semelhantes relaxam tokens apenas de modo controlado, preservando marca/orçamento/requisitos que continuem obrigatórios.

### Imagens e catálogo visual

- Mídias recebidas em burst são agrupadas; links e referências são resolvidos contra o catálogo oficial.
- Reconhecimento visual serve para formar candidatos, não para inventar um produto.
- Variante visualmente incompatível é rejeitada e somente produtos efetivamente enviados entram na memória.

### Carrinho, pedido, PIX e entrega

- Produto/variação são validados antes do carrinho; quantidade é absoluta e idempotente.
- Pedido, PIX e link de pagamento são recuperados por sessão/cliente confirmado, sem recitar URL antiga.
- Webhooks e conciliação financeira preservam ordenação/checkpoint e não avançam sobre falha não processada.
- Transferência para humano exige consentimento e respeita workspace, canal, sessão e proprietário.

### Concorrência, filas e entrega

- Turnos simultâneos da mesma conversa, outbox concorrente e reenvio após confirmação receberam travas e contratos de idempotência.
- Entrega assíncrona sincroniza remarketing somente após confirmação.
- Falha transitória não é convertida silenciosamente em “sem produto” ou “sem pedido”.

### OpenAI, ferramentas e avaliação

- Responses API é o caminho principal existente; fallback e orçamento de chamadas são limitados.
- `TOOL_SCHEMAS` expõe ao modelo somente a superfície de leitura aprovada. O registro interno inclui capacidades de mutação, mas elas permanecem fora do loop livre do modelo.
- Guardrails determinísticos ficam junto de fatos e efeitos colaterais; revisores generativos não substituem validação de contrato.
- Traces, campanhas de qualidade, replay de conversas, conjuntos fechados e comparação controlada permitem medir seleção de ferramenta, fidelidade factual e regressões.
- O catálogo de capacidades tinha uma política duplicada de recuperação de pagamento; a duplicação foi removida.

## Conformidade Tray verificada

- Base por loja com `/web_api` e `access_token` como query parameter na chamada upstream.
- Renovação OAuth centralizada, single-flight e expiração conservadora quando a Tray omite data.
- Payloads administrativos envolvidos na chave do recurso (`Product`, `Order`, `Cart` etc.).
- Paginação máxima de 50, tratamento de 401/429/5xx e rate limit centralizado.
- `ProductsSold` permanece no contrato de pedido e cancelamento usa o caminho oficial.
- Cotação usa CEP normalizado e produtos indexados; resposta vazia de frete é estado legítimo.
- Webhook é form-urlencoded, deduplicável e seguido de consulta complementar quando necessário.
- Nenhum acesso direto à REST administrativa da Tray foi encontrado no NSAgent.

## Gates antes de publicar

1. Rodar novamente as duas suítes completas e o lint.
2. Executar `docs/integration_smoke_test.md` contra os serviços publicados.
3. Em loja de teste, validar buscas com `property_name/value` e `property_id/value_id`, produto com variações e uma marca com mais de quatro páginas.
4. Conferir que filtros de safira/mecanismo/caixa/orçamento não são perdidos no trace e que a variante selecionada mantém preço/estoque próprios.
5. Validar criação de carrinho e pedido apenas com dados de teste, observando reconciliação e ausência de duplicidade.
6. Acompanhar 401, 429, latência, chamadas por turno, taxa de fallback, rejeições factuais e resultados vazios pós-deploy.

## Limites e pendências reais

- Os 7 testes pulados precisam de flags, credenciais ou serviços externos; execute-os no ambiente autorizado antes de um rollout amplo.
- Há 8 avisos de depreciação do adaptador padrão de data do `sqlite3`; não afetam a suíte hoje, mas devem ser removidos antes de uma atualização futura de Python.
- O plugin Tray contém resumos estáticos que podem divergir da documentação viva. A busca MCP/documentação oficial deve prevalecer; foi observada divergência histórica no caminho de cancelamento.
- O MCP Storefront é público e orientado à vitrine. Seu uso futuro deve ser complementar e passar por testes de equivalência/frescura antes de entrar no caminho comercial.
- Resultados locais não provam saúde de Render, Vercel, Supabase, Brevo, Meta, Mercado Pago ou da loja Tray em produção.

## Documentação mantida separadamente

- `README.md`: instalação e visão geral do serviço.
- `docs/AVALIACAO-HISTORICO.md` e `docs/REGRESSAO-CONVERSACIONAL.md`: execução das avaliações.
- `docs/contextual-discovery.md`: operação da descoberta guiada.
- `docs/integration_smoke_test.md`: validação de integração publicada.
- `docs/instagram_story_*.md` e `docs/image_catalog_incident_2026-09-16.md`: fluxo multimodal e incidente específico.
- `docs/release_packaging.md` e `docs/security_incident_oidc.md`: release e segurança.
- `docs/plano_evolucao_omnichannel_instagram.md`: roadmap ainda não encerrado.
- `knowledge/newstore/*.md`: conhecimento de negócio usado pelo agente.

## Fontes oficiais usadas nesta consolidação

- OpenAI Agents: https://developers.openai.com/api/docs/guides/agents
- OpenAI tools: https://developers.openai.com/api/docs/guides/tools
- OpenAI guardrails e revisão humana: https://developers.openai.com/api/docs/guides/agents/guardrails-approvals
- OpenAI avaliação de agentes: https://developers.openai.com/api/docs/guides/agent-evals
- Tray Developers, API Plugin, MCP Storefront e REST: https://developers.tray.com.br/
