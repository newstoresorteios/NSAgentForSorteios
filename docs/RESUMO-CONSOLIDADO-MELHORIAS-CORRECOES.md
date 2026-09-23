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

### Execução dos quatro gates em 23/09/2026

Esta seção complementa a validação local anterior. Os gates **não estão todos
aprovados**; ausência de credencial ou telemetria não conta como sucesso.

| Gate | Evidência obtida | Estado e pendência |
| --- | --- | --- |
| 1. Smoke publicado | NSAgent `e81eec6535b1`, TRAYadaptor `817cdc0` live no Render; saúde dos três serviços HTTP 200; OAuth Tray válido; catálogo autenticado HTTP 200, sem token HTTP 401; Supabase acessível | Parcial: falta executar `/api/integrations/tray/test` com `ADMIN_API_TOKEN` do NSAgent |
| 2. Jornada comercial | Testes locais de carrinho, variante, frete, pedido, PIX, reconciliação, webhooks e bloqueio de efeitos reais aprovados na suíte direcionada | Parcial: não houve compra ponta a ponta em loja de homologação nem cobrança de teste; faltam configurações desse ambiente |
| 3. Carga e falhas | 100 turnos locais em 10 conversas: sem sobreposição por conversa, 94 conclusões únicas e 6 timeouts injetados com liberação das travas; testes de falhas de integrações aprovados | Parcial: prova concorrência local, não capacidade comercial em produção; faltam carga sustentada e falhas dos provedores em homologação |
| 4. SLOs e alertas | Avaliador determinístico criado; regressões de limites, dados ausentes, recuperação e incidente crítico aprovadas; alerta de isolamento corrigido para primeiro evento | Parcial: faltam agregação persistente, sinais críticos completos, janela representativa e entrega de notificações operacionais |

Validação desta etapa: 800 testes direcionados aprovados, 1 pulado (piloto SDK
opcional), 8 avisos SQLite; TRAYadaptor com 239 aprovados. O smoke foi reforçado
com consulta autenticada ao catálogo e consulta NSAgent → adaptador. Falta de
token agora impede a aprovação. Os SHAs de produção acima correspondem ao
instante da coleta; estas melhorias exigem confirmação do novo deploy.

O bloqueio de CI `undocumented Settings aliases: AGENT_IMAGE_SEARCH_DETAIL`
foi corrigido documentando `AGENT_IMAGE_SEARCH_DETAIL=high` em `.env.example`,
igual ao padrão do runtime. Contrato de 233 aliases, scanner de segredos e
empacotamento dry-run aprovados. O placeholder do token administrativo na
documentação também foi ajustado ao formato reconhecido pelo scanner.

**Custo de modelo nesta etapa: zero chamadas à OpenAI.** A Vercel confirmou a
presença dos segredos, mas bloqueou seu download. Não foi criada outra chave.
O token interno Tray foi usado em memória a partir da integração existente;
nenhum segredo ou conteúdo de cliente entrou no relatório.

**Amostra operacional:** seis GETs de saúde por serviço, concorrência dois,
18/18 sem erro. p95 medido deste computador: NSAgent 855 ms, Tray 1.142 ms,
Chatbo 1.241 ms. Isso inclui rede e não mede o tempo de resposta do agente.
Filas no instante consultado: 228 entradas processadas, 143 saídas enviadas,
nenhuma pendente. O adaptador não apresentou logs de nível `error` no retorno
consultado; o Chatbo teve um registro ASGI em 22/09, sem diagnóstico nesta etapa.

Nos seis atendimentos das últimas 24 horas, todos tiveram entrega e validação
factual interna aprovadas, e um registrou `llm_budget_exceeded` (16,7%). O maior
tempo foi 53.843,67 ms: 3 chamadas OpenAI, 11 Tray, 35.721 tokens de entrada e
4.101 de saída, com cerca de 12,7 s na revisão da resposta. Esse atendimento
ocorreu às 15:51 UTC, antes do deploy consolidado. Os seis registros não são
uma certificação da versão atual nem uma amostra suficiente para aprovar SLOs.

**Metas operacionais iniciais (devem ser aferidas por workspace/deployment):**

- Disponibilidade: 99,5% em 30 dias; alerta com três falhas consecutivas de smoke.
- Resposta comercial: p95 até 20 s; avaliar janelas de 15 minutos com pelo menos
  20 turnos válidos. Registrar separadamente fila, rede e processamento.
- Fallback: até 10%; rejeição factual interna: até 2%, na mesma janela.
  Rejeição pelo validador não equivale a erro factual entregue ao cliente.
- Isolamento, pedido duplicado ou pagamento confirmado indevidamente: tolerância
  zero e incidente imediato, sem esperar mínimo de amostras.
- As métricas de incidente crítico ainda não estão completas em `turn.quality`;
  sua ausência deve continuar como inconclusiva, não como zero ocorrências.

`scripts/evaluate_operational_slos.py <eventos.jsonl>` avalia exports locais de
`turn.quality`: código 0 aprovado, 1 violação, 2 inconclusivo. Não faz chamadas
externas. `scripts/probe_release_gates.py` executa smoke e amostra limitada de
saúde. Estes executores não instalam agendamento nem canal de notificações.
Os alertas existentes em `app/ops/rollout.py` usam janela em memória por processo;
por isso não substituem uma agregação persistente em ambiente serverless.

Para encerrar os gates restantes: fornecer o caminho do arquivo privado de
acesso administrativo (JSON com `token`) e a configuração da loja/provedor de
pagamento de teste; executar poucos cenários remotos selecionados sem reparo
generativo; validar a jornada real de homologação; medir carga sustentada e
confirmar o destino de alertas. Não enviar tokens pelo chat.

- Os 7 testes pulados precisam de flags, credenciais ou serviços externos; execute-os no ambiente autorizado antes de um rollout amplo.
- Há 8 avisos de depreciação do adaptador padrão de data do `sqlite3`; não afetam a suíte hoje, mas devem ser removidos antes de uma atualização futura de Python.
- O plugin Tray contém resumos estáticos que podem divergir da documentação viva. A busca MCP/documentação oficial deve prevalecer; foi observada divergência histórica no caminho de cancelamento.
- O MCP Storefront é público e orientado à vitrine. Seu uso futuro deve ser complementar e passar por testes de equivalência/frescura antes de entrar no caminho comercial.
- Resultados locais não provam saúde de Render, Vercel, Supabase, Brevo, Meta, Mercado Pago ou da loja Tray em produção.

## Correções da conversa — 23/09/2026

- Atributos de identificação `qual:*` não contam mais como preferências do produto.
- Pedido genérico volta à qualificação contextual antes de pesquisar; busca preliminar
  vazia e sem requisitos técnicos não autoriza recomendar um produto arbitrário.
- A busca de relógios rejeita títulos claramente de acessórios sem afetar pedidos
  explícitos de pulseiras ou relógios cuja descrição menciona uma pulseira.
- A regressão multi-turno preserva os marcadores de perguntas e o cache da descoberta.
- Migração aditiva `20260923185104` restaura sete definições de configuração ausentes;
  aplicada ao banco em 23/09, sem sobrescrever personalizações. Os dois blocos de
  política da persona foram renderizados com sucesso após a restauração.
- Continuidade: responder o orçamento mantém a qualificação aberta até cobrir uso
  ou preferência, respeitando o limite de perguntas publicado. O marcador da pergunta
  tem precedência sobre uma classificação incorreta de ausência de contexto.
- Indiferença: "não tenho modelo em mente" não significa ausência de preferência
  de cor, material ou ocasião; esses descartes precisam de suporte na conversa.
- Avaliador: URLs de imagens dentro de texto não contam como entrada multimodal;
  bloqueios de orçamento ficam explícitos e interrompem a campanha. A reserva usa
  um limite superior conservador do payload textual efetivo, sem liberar reservas
  em caso de timeout. A CLI pode reutilizar o login Vercel sem exportar credenciais.
- Presente não é objeção de aprovação: a substring `e presente` capturava
  `de presente`; destinatário sozinho também não autoriza pular a qualificação.
- Continuidade normaliza `relógio`/`relógios`; uma resposta curta à pergunta de
  orçamento recupera a marca da consulta anterior quando o intérprete a omite.
- Validação local: 2.400 testes aprovados, 7 pulados; secret scan e contrato de
  configuração aprovados. Houve um caminho feliz real de cinco turnos, incluindo
  link correto, mas repetições revelaram novas falhas. Não é certificação de produção.
- A avaliação real permanece requisito para liberação ampla. Testes isolados não
  certificam entrega WhatsApp, checkout nem pagamento real.

### Sondagem real de catálogo — Seiko SRPL13K1

- Matriz reproduzível: `evals/catalog-srpl13k1-scenarios.json`.
- Evidência local: `docs/audits/2026-09-23/catalog-srpl13k1-evidence.json`;
  11 turnos reais, incluindo falhas, repetição corrigida e bloqueios de orçamento.
  OpenAI e catálogo reais, em deploy candidato isolado; sem envio a clientes ou pedidos.
- Identificação progressiva: apenas Seiko → pergunta orçamento → R$ 6.500.
  A primeira execução perdeu a marca e ofereceu Citizen. A repetição após proteção
  local ofereceu Seiko, mas não fez nova pergunta. Ao acrescentar Samurai, mostrador
  preto e pulseira de aço, encontrou SRPL13K1 com preço e link corretos, sem receber
  antecipadamente essa referência. Acerto pontual não demonstra consistência.
- Falha P1: perguntas factuais viram filtros obrigatórios. `Tem safira?` não recebe
  a correção Hardlex; `tem 38 mm?` busca Baby Alpinist em vez de explicar o Samurai.
  A primeira resposta não confirma safira: a falha é não corrigir a premissa.
- Falha P1: consulta completa por atributos e confirmação posterior da ficha
  retornam fallback genérico apesar de o detalhe correto estar nas ferramentas.
  Investigar ordenação dos filtros, preservação de identidade e confirmação técnica.
- Falha P1: teto de R$ 5.000 inclusive Pix dispara oferta de atendimento humano
  em vez de informar que R$ 5.184,99 supera o limite. Não houve desconto inventado.
- Índice local retornou zero para a referência conhecida em várias rodadas;
  o adaptador encontrou o produto 11989. Investigar recuperação exata no cache miss.
- Avaliador: 253 fichas de candidatos inflaram uma entrada além do limite reservado;
  notas automáticas de orçamento corrigido e tamanho ficaram inconclusivas.
  Necessária compactação de candidatos preservando evidência detalhada do produto.
- Cor azul e pronta entrega: campanha interrompeu chamadas por orçamento, portanto
  inconclusivos. A resposta visível de prazo informou corretamente 30 dias úteis,
  mas não certifica a execução completa. Não tratar bloqueio de custo como falha
  funcional do agente nem como aprovação.
- Teto autorizado US$ 12; reservas acumuladas US$ 11,985351 ao encerrar esta rodada.
  Reserva conservadora não é fatura/gasto efetivo. Novas chamadas exigem ampliação.
- Bloqueios para liberação ampla: identidade exata, resposta factual versus filtro,
  recuperação de confirmação técnica e repetição completa sem bloqueio de avaliação.
  Esses pontos permanecem abertos; não foram corrigidos só por registrar a sondagem.

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
