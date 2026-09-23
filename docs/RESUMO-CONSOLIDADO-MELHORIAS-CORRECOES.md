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
- Validação local: 2.425 testes aprovados, 7 pulados; secret scan e contrato de
  configuração aprovados. Houve um caminho feliz real de cinco turnos, incluindo
  link correto, mas repetições revelaram novas falhas. Não é certificação de produção.
- A avaliação real permanece requisito para liberação ampla. Testes isolados não
  certificam entrega WhatsApp, checkout nem pagamento real.

### Sondagem real de catálogo — Seiko SRPL13K1

- Matriz reproduzível: `evals/catalog-srpl13k1-scenarios.json`.
- Evidência local: `docs/audits/2026-09-23/catalog-srpl13k1-evidence.json`;
  27 turnos reais, incluindo falhas, repetições corrigidas e bloqueios de orçamento.
  OpenAI e catálogo reais, em deploy candidato isolado; sem envio a clientes ou pedidos.
- Identificação progressiva: apenas Seiko → pergunta orçamento → R$ 6.500.
  A primeira execução perdeu a marca e ofereceu Citizen. A repetição após proteção
  local ofereceu Seiko, mas não fez nova pergunta. Ao acrescentar Samurai, mostrador
  preto e pulseira de aço, encontrou SRPL13K1 com preço e link corretos, sem receber
  antecipadamente essa referência. Acerto pontual não demonstra consistência.
- Corrigido: referências compactas (SRPL13K1/SPB155) são aceitas quando explícitas;
  calibre 4R35 não vira SKU. Inspeção resolve a identidade exata sem usar a premissa
  do cliente como filtro. Repetições `fixed-wrong-glass` e `fixed-wrong-size` passaram:
  Hardlex, não safira; 41,7 mm, não 38 mm. Não substitui silenciosamente a referência.
- Corrigido: diâmetro decimal é separado de espessura, lug-to-lug e largura da pulseira.
  A ficha diverge entre 41 mm no resumo e 41,7 mm na descrição; a resposta de
  `round18-specs` sinalizou essa divergência e passou. O catálogo não foi alterado.
- Corrigido: o contrato de oferta comparava a ficha completa com sua projeção
  factual autorizada e apagava respostas válidas. Compara agora a mesma projeção,
  mantendo bloqueio quando preço/identidade realmente mudam. `verified-all-features`
  encontrou o SRPL13K1 por características completas, sem fornecer o SKU, e passou.
- Corrigido: consulta de orçamento com Pix não deve virar política genérica de
  pagamento nem carrinho. Uma repetição tentou `create_cart`, BLOQUEADO pela
  avaliação; nenhum carrinho foi criado. Itens inferidos em uma inspeção, sem ação
  explícita de compra, não autorizam criar carrinho. `round18-budget` passou:
  informou R$ 5.184,99 no Pix acima de R$ 5.000, sem desconto inventado ou mutação.
- Corrigido: critérios da pergunta contextual são preservados. Conselho e segunda
  checagem distinguem uma explicação negativa de uma oferta incompatível, apenas
  para inspeção de uma ficha revalidada. `round18-color-fixed` passou: SRPL13K1 é
  preto e não atende azul; demais atributos e teto preservados. Não apresentou
  outra referência azul: essa busca alternativa ainda não foi executada.
- Prazo: `final-availability` passou, informando 30 dias úteis sem prometer pronta
  entrega/chegada amanhã nem criar pedido. Estoque positivo não prova pronta entrega.
- Avaliador: candidatos não selecionados são compactados; detalhe completo é
  preservado para produtos consultados/selecionados. A entrada antes inflada por
  253 fichas foi reduzida de cerca de 583 KB para 160 KB, sem alterar fatos originais.
- Teto autorizado ampliado para US$ 18. Ao encerrar as sondagens Seiko, reservas
  acumuladas US$ 16,99270875. Reserva conservadora não é fatura/gasto efetivo.
  Campanhas anteriores estão paradas; a jornada Citizen tem teto adicional de
  US$ 1 e 12 chamadas. Não retomar saldos antigos em paralelo: ultrapassaria o limite.
- Evidências aprovadas são repetições pontuais, em candidatos corrigidos distintos,
  não uma certificação de consistência estatística nem execução WhatsApp ponta a ponta.
  Persistem otimização de filtros de cor pouco estruturados, latência e recuperação
  de cache. Homologação de checkout/pagamento e rollout monitorado continuam necessários.

### Segunda marca — Citizen

- Produto de referência: Citizen Promaster Marine NY0120-01EE; matriz em
  `evals/catalog-citizen-ny0120-scenario.json`. Iniciar sem código, revelar orçamento
  e preferências conforme perguntas. Não reaproveitar contexto do Seiko.
- Primeira jornada: perguntou orçamento, mas recomendou após conhecer apenas marca
  e preço. Após receber os atributos, retornou limitação genérica e não identificou
  o produto. O juiz automático aprovou por ausência de invenção; revisão manual
  classifica o objetivo de identificação como FALHA, não como caminho feliz.
- Causas corrigidas: amostra preliminar sem facetas não encerra a entrevista após
  o orçamento; negação curta `não Eco-Drive` não vira preferência por solar;
  busca técnica por mecanismo/cor não depende também da propriedade esparsa `Cor`.
  Os demais filtros e revalidação de detalhes continuam obrigatórios.
- Teto ampliado explicitamente para US$ 20. Antes da repetição, reservas anteriores
  US$ 17,92291425; campanha nova limitada a US$ 2, sem retomar campanhas antigas.
- Resultado da rodada até US$ 20: FALHA de identificação, apesar da qualificação corrigida.
  A sequência `validated-01` → `validated-02` perguntou orçamento e modelo;
  a terceira resposta não encontrou o Citizen. `final-03` repetiu o último turno
  com histórico real na versão corrigida, explicitamente registrando a mudança
  de deployment, e também falhou. Não existe caminho feliz Citizen comprovado.
- Corrigidos ainda: calibre numérico 8204 extraído como SKU e preferência por
  alias inglês `black` antes da cor informada `preto`. Os testes locais cobrem
  essas causas, mas a repetição real ainda retornou busca vazia.
- Evidência restante: buscas `Promaster Automático Preto` e `Promaster Automático`,
  Citizen e faixa 0–3500, retornaram zero; fichas revalidadas eram de outros
  modelos, com aço/safira ou preço incompatível. Consulta somente leitura ao índice
  não encontrou NY0120-01EE. Isso não prova indisponibilidade na Tray: falta
  comparar consulta por referência sem filtros, busca por tokens e preço promocional.
  O OpenAPI atual confirma GET `/internal/products/search` com tokens/brand/filtros.
- Dez turnos Citizen preservados em `docs/audits/2026-09-23/catalog-citizen-evidence.json`.
  Divergências do juiz estão em `catalog-manual-review.json`: não repetir a marca
  na frase não é perdê-la do estado; fallback sem encontrar produto não é sucesso.
- Pausadas chamadas dessa rodada com reserva total US$ 19,68371475, abaixo de US$ 20.
  Não é o valor faturado. Campanhas anteriores não devem ser retomadas sem
  recontar a reserva global; não há testes automáticos agendados.
- Gate de liberação ampla continua NÃO aprovado: resolver e repetir identificação
  Citizen, além de homologar o canal real e checkout/pagamento. Os casos Seiko
  aprovados não generalizam para todas as marcas ou para todas as repetições.

### Continuação autorizada até US$ 22

- Consulta real pela referência encontrou NY0120-01EE, id 11013: preço atual
  R$ 3.399,99, Pix R$ 2.889,99, mineral, borracha, disponibilidade em 30 dias úteis.
  Portanto a ausência no índice parcial não era prova de ausência no catálogo.
- A resposta correta dessa consulta foi bloqueada por orçamento fictício de R$ 120:
  o extrator pegava números do SKU e aceitava `ate` dentro de `material` como
  indicação de teto. Corrigido para exigir indicação monetária junto ao valor ou
  expressão de limite com fronteira de palavra; coberto com regressões locais.
- A consulta técnica agora usa tokens AND no contrato existente do adaptador,
  permitindo palavras intermediárias como `Marine`. Nessa recuperação específica,
  não envia faixa upstream: o preço de tabela pode excluir uma promoção válida.
  O teto permanece obrigatório nos filtros locais sobre preço vigente e na
  revalidação da ficha. Testes verificam aceitação de R$ 3.399,99 e rejeição de
  R$ 3.599,99 com teto R$ 3.500, nos modos exato e recomendação.
- Campanha isolada `launch-20260923-citizen-tokens22`, limite US$ 2 / 24 chamadas,
  sobre reserva anterior US$ 19,68371475. Campanhas antigas permanecem paradas.
  Esse limite conservador conjunto fica abaixo do teto total aprovado de US$ 22.
- A repetição `happy22-01` → `happy22-02` qualificou marca/orçamento/modelo, mas
  `happy22-03` ainda falhou na identificação. A consulta direta
  `reference22-fixed` passou: orçamento fictício eliminado, ficha correta respondida.
- Diagnóstico Render: `/internal/products/search` recebia `available=true` e
  `available_in_store=true`. No código do adaptador, a rota de listagem converte
  booleanos para `1/0`, mas a rota de tokens não. Cliente do NSAgent corrigido
  para enviar `1/0` nessa rota, preservando os filtros em vez de removê-los.
  MCP Tray consultado: a documentação contém tabelas contraditórias sobre a
  semântica de `available_in_store`; não invertemos o significado desse campo.
- Suíte local: 2.441 passaram, 7 ignorados; contrato de configuração (233 aliases)
  e varredura de segredos passaram. Regressões cobrem normalização true/false,
  booleanos e strings, sem alterar os valores já numéricos.
- Reserva anterior à última repetição: US$ 21,39451275. Tranche anterior parada;
  nova `launch-20260923-citizen-flags22` limitada a US$ 0,60 / 7 chamadas, mantendo
  reserva máxima conjunta US$ 21,99451275. Não executar tranches antigas em paralelo.
- A repetição `happy22-03-flags-fixed` confirmou a correção de transporte: consulta
  com `1/0` retornou 20 produtos, incluindo NY0120-01EE. Ainda falhou na resposta:
  detalhes de três outros modelos consumiram a cota antes da ficha correta.
  Corrigida a priorização de detalhes por preço vigente compatível antes do corte
  de ranking; valores desconhecidos vêm depois, fora do teto por último. Todos os
  fatos continuam exigindo revalidação. Regressão cobre promoção após 20 itens caros.
- Reserva acumulada US$ 21,78337575. Usuário autorizou US$ 25 e pediu nova marca/modelo
  e push. Não houve chamada na tranche `citizen-detail23`; última correção Citizen
  validada apenas localmente, sem declarar caminho feliz completo aprovado.
  Nova campanha `launch-20260923-orient25`: US$ 3,20 / 36 chamadas; máximo conjunto
  US$ 24,98337575, mantendo campanhas anteriores paradas. Evidência Citizen: 17 turnos.
- Suíte completa após priorização de detalhes: 2.442 passaram, 7 ignorados.

### Terceira marca — Orient M-Force Land

- Referência escolhida para conferência independente: RA-AC0N02Y10B, mostrador
  laranja, automático F6722, safira, aço, 200 m, reserva 40 h, caixa 45 mm,
  espessura 13,2 mm e pulseira 20 mm. Matriz em `evals/catalog-orient-mforce-scenario.json`.
- Push `b7a6c0d` publicado e CI aprovado. Primeira conversa Orient: `01` perguntou
  orçamento; `02` recomendou imediatamente após R$ 4.000. Mesmo recomendando a
  referência escolhida por coincidência, o caso FALHOU por qualificação prematura.
- Causa: atributo interno `somente:Orient` foi contado como preferência adicional
  suficiente. Ajuste ignora marcador de exclusividade de marca nessa decisão;
  mantém preferências reais e o pedido explícito de busca. Coberto por regressão.
- Push adicional `e228040` aprovado no CI e publicado. Nova conversa inteira no
  mesmo candidato: `final-01` → `final-02` → `final-03` → `final-04-specs`, todos
  aprovados. Perguntou orçamento e modelo, identificou a referência sem receber
  seu código e depois confirmou cada especificação solicitada, preço Pix e prazo.
  Escopo: OpenAI real + Tray real, avaliação isolada; não WhatsApp ponta a ponta.
- Negativos separados falharam: `negative-color-glass` perdeu a explicação de
  safira versus mineral; `negative-budget-deadline` foi para humano sem necessidade.
  As respostas originais corretas foram rejeitadas pelo verificador: negação
  `Não.` em frase separada não era reconhecida como incompatibilidade de cor;
  `R$` exigia erroneamente fronteira de palavra após `$`, ignorando o contexto
  de orçamento. Regras ajustadas mantendo exigência de ficha única revalidada e
  confirmação da cor realmente declarada na ficha; testes rejeitam aceitação falsa.
- Reteste Citizen `detail25-final` encontrou NY0120-01EE, preço R$ 3.399,99,
  Pix R$ 2.889,99 e link. Objetivo de recuperação corrigido; critério completo do
  juiz ainda FALHOU porque a resposta não enumerou borracha/mineral/200 m/8204/42 h.
  Não tratar identificação correta como aprovação de toda a cobertura técnica.
- Reserva US$ 24,38296650 antes da última verificação. Campanha Orient anterior
  parada; tranche `launch-20260923-orient-final25` com US$ 0,61 / 7 chamadas mantém
  máximo global US$ 24,99296650. Reserva conservadora não é custo faturado.
- `negative-combined-fixed` ainda falhou: o texto correto usou `passa de R$ 3.000`
  e `Mostrador: é laranja, não azul`, variantes não reconhecidas pelas regras.
  Acrescentada regressão com a resposta real e suporte às duas construções, sem
  permitir afirmar a cor solicitada como verdadeira. Reserva após esse teste:
  US$ 24,71005350. Restam no máximo 3 chamadas / US$ 0,282913 na tranche ativa.
- Encerramento: `negative-combined-final` INCONCLUSIVO, com
  `evaluation_campaign_budget_exceeded_or_changed` no juiz e resposta final de
  encaminhamento humano. A correção final das regras tem regressão local, mas
  não aprovação real desse caso combinado. Não encerrar esse gate como sucesso.
- Reserva TOTAL final: US$ 24,95149725, abaixo do teto US$ 25. Sem novas chamadas
  pagas, sem testes agendados. Tranches encerradas operacionalmente, não retomar
  seus saldos antigos em paralelo. Não equivale a custo faturado pela OpenAI.
- Última suíte completa: 2.446 testes passaram, 7 ignorados, 8 avisos legados;
  contrato de configuração e varredura de segredos aprovados. Evidências exportadas:
  10 turnos Orient e 18 Citizen, incluindo falhas e repetições separadamente.
- Estado atual: caminho feliz Orient aprovado em quatro turnos na mesma versão;
  recuperação Citizen corrigida, cobertura explícita de todos os atributos ainda
  pendente; negativo combinado Orient precisa repetição com orçamento disponível.
  Canal WhatsApp ponta a ponta, checkout/pagamento e consistência em repetições
  continuam fora desta comprovação. Não declarar produção sem desvios ou 100% pronta.

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
