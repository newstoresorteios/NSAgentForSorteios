# Auditoria integrada pós-deploy — NewStoreAgent v100

**Data da observação:** 08/09/2026, entre 15:20 e 15:46 BRT

**Código auditado:** `bd7be8fb2aec43cdd0b364744422d69e26a19b7f` (`fix/close-inspected-watch`)

**Produção observada:** `openai-db-context-multichannel-runtime-v100`

**Serviços:** NewStoreAgent/Vercel, TRAYadaptor/Render, PostgreSQL e Brevo
**Documento anterior usado como evidência:** `Auditoria_Chatbo_2026-09-07.pdf` (73 páginas, referência v90)

## Parecer

O projeto evoluiu de forma substancial desde a auditoria de 07/09. A v100 contém correções reais para contexto de marca, recomposição de recomendações, autoridade de consulta, concorrência de entrada e saída, PIX, validação factual, segurança HTTP e aprendizagem. A suíte local no commit implantado está integralmente verde: **1.879 testes aprovados e 1 desmarcado**, em 20,77 segundos. Os serviços públicos responderam corretamente em todas as sondagens e as 13 mensagens das últimas 24 horas tiveram resposta persistida e envio confirmado.

Ainda não existe evidência suficiente para declarar a v100 validada em produção. A última conversa real ocorreu às 09:51 BRT, várias horas antes da promoção da v100. Portanto, todas as conversas recentes usadas nesta análise foram processadas pela v78. Elas demonstram os defeitos que motivaram as correções, mas não demonstram que a versão nova os resolveu no ambiente real.

Há três riscos operacionais imediatos:

1. **A produção está em um branch divergente e não em `main`.** O branch implantado está nove commits à frente e três merge commits atrás de `main`, não há pull request aberto e o SHA `bd7be8f` não executou a CI completa do GitHub. Um novo deploy de `main` pode recolocar a versão anterior.
2. **A aprendizagem contínua está quebrada operacionalmente.** Todas as execuções agendadas recentes do workflow `Attendance Learning` falharam no passo que chama o endpoint. O cursor do banco segue sem `last_response_id`, a última execução varreu zero linhas e há apenas 11 revisões nos últimos sete dias.
3. **A observabilidade ainda não fecha a cadeia Vercel → agente → TRAYadaptor.** O código gera `trace_id`, mas não propaga esse identificador ao adaptador nem persiste os IDs de requisição da Vercel e do Render. Os logs brutos dos dois provedores não puderam ser exportados nesta auditoria por falta de uma credencial válida de leitura; a análise operacional usou health checks, cabeçalhos, GitHub Actions e registros do banco.

Minha classificação atual é **implementação avançada, produção estável no transporte observado, qualidade da v100 ainda em canário não medido**. O maior retorno agora vem de corrigir implantação, telemetria e validação pós-deploy. Outra reescrita ampla do agente teria risco maior e benefício menor.

## Evidências e limites

Foram usadas cinco camadas de evidência:

| Camada | Evidência obtida | O que permite concluir |
|---|---|---|
| Código | AST de todos os 253 arquivos Python em `app`, leitura dirigida dos fluxos críticos, configurações, SQL, scripts e workflows | Responsabilidades, acoplamentos, caminhos de erro e contratos implementados |
| Testes | Resultado integral salvo em `pytest-final.txt`; coleta atual de 1.880 casos; scanner de segredos e empacotamento dry-run | Regressões offline e contratos determinísticos no SHA implantado |
| Banco de produção | Consultas somente leitura, com timeout, sobre mensagens, respostas, filas, catálogo, memória, reviews, prompt audit e aprendizagem | Comportamento persistido e resultado de entregas; não mostra tudo o que ocorreu antes de uma falha de persistência |
| Serviços públicos | Oito probes consecutivos em cada health check; cabeçalhos Vercel/Render; versão pública | Disponibilidade e configuração básica no instante observado |
| Plataformas | API pública do GitHub Actions e páginas oficiais de Vercel/Render | Estado de CI/agendamentos e critérios de observabilidade dos provedores |

Os Runtime Logs da Vercel e os logs do Render exigem acesso autenticado ao projeto. O token OIDC local não foi aceito pela API da Vercel e não havia chave da API do Render. A documentação da Vercel confirma que o Runtime Logs oferece `requestId`, `traceId`, duração, memória, deployment e agrupamento por requisição; a documentação do Render oferece busca por `Rndr-Id` e request ID.[^1][^2] A ausência desse acesso limita a atribuição de latência entre Vercel, OpenAI, banco, Brevo e Render. Não invalida as constatações obtidas no banco, mas impede afirmar a causa de cada atraso ou exceção.

Nenhuma mensagem foi enviada, nenhum checkout foi criado e nenhuma linha do banco foi alterada. As consultas de produção usaram transação read-only. Não foram feitas chamadas pagas ao modelo durante esta auditoria.

## Estado operacional observado

### Disponibilidade e versão

| Serviço | Resultado de 8 probes | Latência média | P95 da amostra | Evidência adicional |
|---|---:|---:|---:|---|
| NewStoreAgent `/api/health` | 8/8 HTTP 200 | 271,8 ms | 693 ms | v100, produção, DB configurado, dry-run desativado |
| TRAYadaptor `/health` | 8/8 HTTP 200 | 363,9 ms | 711 ms | build textual `quota-webhook-2026-09-06` |
| TRAYadaptor `/health/tray` | 8/8 HTTP 200 | 258,1 ms | 288 ms | loja 687890, acesso válido, refresh bootstrap e cache DB configurados |

O token Tray observado expiraria às 16:17:48 BRT do mesmo dia. Ele ainda estava válido no instante do teste. A renovação após expiração não foi exercitada, então a existência do bootstrap não prova que o próximo refresh ocorrerá corretamente.

O health do agente agora expõe somente estado básico, o que reduz vazamento de configuração. Diagnósticos detalhados permanecem protegidos por token administrativo em `app/http/health.py`. As rotas de teste e echo também exigem `verify_admin_token`, e o hardening correspondente está coberto por testes.

### Conversas e entrega

| Janela | Entradas | Respostas | Envio confirmado | Handoffs | P50 entrada→persistência | P95 | Máximo |
|---|---:|---:|---:|---:|---:|---:|---:|
| 24 horas | 13 | 13 | 13 | 0 | 11,41 s | 44,47 s | 46,82 s |
| 7 dias | 91 | 90 | 90 | 1 | 14,58 s | 47,19 s | 67,74 s |

O transporte das últimas 24 horas funcionou: não há inbox pendente, falha ou dead letter; as 166 linhas da inbox estão em `processed`; não havia linha na outbox. Em sete dias existe uma entrada sem resposta: em 04/09, a mensagem “Até 3500” foi persistida, mas não possui resposta nem linha correspondente na inbox. As duas mensagens imediatamente anteriores da conversa foram respondidas. Isso é compatível com o tipo de perda que as correções novas de serialização, accepted outbound e idempotência procuram fechar, mas a v100 ainda não foi submetida a uma conversa real para comprovar o resultado.

A latência de 44–47 segundos no P95 é alta para atendimento conversacional e hoje ocorre dentro do request quando `agent_async_ingress_enabled=false`. Essa configuração evita que mensagens aguardem o cron diário de inbox, porém mantém o webhook exposto a toda a soma de OpenAI, Tray, banco e envio. O caminho assíncrono não deve ser ligado isoladamente: `/api/cron/process-inbox` está agendado apenas uma vez por dia em `vercel.json`. Para habilitá-lo com segurança é preciso um worker frequente ou fila externa, SLO de tempo até primeira resposta e monitoramento de backlog.

### Resultado das respostas recentes

Nas últimas 24 horas, os motivos persistidos foram:

| Motivo | Respostas | Leitura |
|---|---:|---|
| sem bloqueio (`ok`) | 6 | Resposta normal |
| `commerce_clarification` | 4 | Pergunta de qualificação ou falta de evidência |
| `answer_council_blocked` | 3 | Conselho vetou a resposta; nos casos observados, produziu falso “não encontrei” |

Em sete dias, 44 de 90 respostas ficaram sem bloqueio, 22 foram clarificações, 10 falharam na validação factual e 7 foram bloqueadas pelo conselho. Esses bloqueios demonstram que as barreiras estão ativas, mas também mostram custo de falso negativo. A v100 passa a recompor a lista com candidatos individualmente elegíveis antes de desistir, o que é uma correção melhor do que enfraquecer o validador.

As conversas mais recentes mostram dois comportamentos distintos:

- Às 09:48–09:51 BRT, a conversa de relógios até R$ 5.000 manteve o objetivo, ofereceu três Seiko coerentes e respondeu ao pedido de foto do primeiro item com link do produto. Este fluxo funcionou na v78.
- Três sequências de orçamento em R$ 2.500, R$ 2.600 e R$ 2.700 terminaram em falso “não encontrei”, incluindo casos marcados `answer_council_blocked`. O índice continha produtos elegíveis. Esse é o defeito principal que a v100 pretende corrigir.
- Em outra conversa, o agente chamou o cliente de “bundinha”. A palavra havia sido enviada em uma conversa de 04/09, foi gravada como memória de contato `recipient` e reapareceu em uma conversa diferente do mesmo contato em 08/09. O termo também foi incorporado aos resumos das duas conversas. As duas memórias encontradas já estavam `superseded`, mas a contaminação já havia alcançado a resposta.

O último caso revela uma lacuna semântica que permanece no código atual: `preferences.recipient` representa ao mesmo tempo o nome pelo qual o cliente quer ser chamado e o destinatário do produto. `contact_preference_memory.py` ainda transforma qualquer `recipient` em memória durável do contato e depois o reidrata em outra conversa. O filtro `_is_plausible_name` elimina algumas expressões e nomes de cor, mas uma palavra sintaticamente válida pode continuar atravessando conversas. A correção recomendada é separar `preferred_name` de `gift_recipient`, exigir introdução explícita para nome durável e nunca promover automaticamente apelidos não confirmados.

### Catálogo e diversidade

O índice possui 1.814 linhas: 320 disponíveis e atualizadas nas últimas 24 horas; as 1.494 linhas antigas estão todas marcadas como indisponíveis. Portanto, a contagem bruta de 1.494 itens stale não representa catálogo vendável desatualizado. O conjunto disponível estava fresco.

O comportamento da imagem anexada é explicado pelos dados:

- Até **R$ 2.500**, só havia **Citizen** entre os produtos disponíveis, com três itens elegíveis.
- Até **R$ 3.000**, havia cinco Citizen, um Seiko e um Tissot.
- Pedir “uma de cada marca nessa faixa” em R$ 2.500 não pode produzir diversidade factual sem flexibilizar preço, disponibilidade ou categoria.

O limite de três sugestões não vem de uma limitação fixa do TRAYadaptor. O agente coleta páginas de 20 itens, pode explorar até cinco páginas/100 produtos e usa pool interno padrão de 20. O limite deliberado da resposta ao cliente é `CUSTOMER_RESULT_LIMIT = 3`, ajustável pela persona até no máximo cinco. A v100 inclui `select_diverse_brand_shortlist`: quando não há marca travada, escolhe marcas distintas antes de preencher vagas repetidas. A estratégia está correta; ela apenas não pode criar marcas que não existem na faixa.

O agente deve responder explicitamente ao limite do estoque: “Até R$ 2.500 encontrei apenas Citizen. Se você aceitar até R$ 3.000, consigo incluir Seiko e Tissot.” Isso mantém o contexto, explica a restrição e oferece uma escolha concreta. A busca deve usar expansão de faixa somente após aceite, sem modificar silenciosamente o orçamento.

## Achados priorizados

| ID | Prioridade | Achado | Evidência | Ação recomendada |
|---|---|---|---|---|
| N01 | P0 operacional | v100 foi promovida de branch divergente; `main` continua na árvore anterior | `bd7be8f` está 9 ahead/3 behind; 0 PR aberto; produção anuncia v100 | Integrar o branch a `main`, executar CI e fazer produção apontar para SHA de `main` |
| N02 | P0 operacional | Aprendizagem agendada falha repetidamente | Todas as execuções recentes do workflow falharam no passo HTTP; cursor sem resposta e `rows_scanned=0` | Corrigir segredo/URL/autorização, tornar o job observável e criar alerta após 2 falhas consecutivas |
| N03 | P0 de validação | Não houve tráfego real pós-v100 | Último inbound 09:51 BRT, deploy validado após 15:20 BRT | Executar canário controlado com conversas reais/sintéticas e revisar registros do turno |
| N04 | P1 | Nome/destinatário atravessa conversas como memória durável | “bundinha” passou de uma conversa para outra via `recipient` | Separar `preferred_name` e `gift_recipient`; confirmar nome antes de memória de contato |
| N05 | P1 | Telemetria não correlaciona serviços ponta a ponta | `trace_id` local não é enviado ao Tray; `Rndr-Id`/Vercel request ID não chegam ao DB | Propagar `X-Request-ID`, guardar IDs de provedor e usar um evento JSON por etapa |
| N06 | P1 | Workflow e cron duplicam a aprendizagem, mas o frequente está inoperante | GitHub pretende 15 min; Vercel roda diariamente; GitHub falha | Escolher um scheduler autoritativo, manter fallback explícito e registrar heartbeat |
| N07 | P1 | Prompt audit e metadata não representam o tráfego recente | 0 compilações em 24h; 13/13 respostas sem `_agent_metadata`, embora tenham context/runtime | Validar primeira resposta v100 e alertar quando taxa de metadata/prompt audit cair abaixo de 99% |
| N08 | P1 | Caminho síncrono tem P95 de até 47 s | Banco de produção, últimas 24h | Medir spans e mover trabalho longo para fila somente quando o consumidor for contínuo |
| N09 | P1 | Reviews e aprendizagem têm amostra pequena e enviesada para falhas | 11 reviews/90 respostas em 7 dias; outcomes apenas `unclear` e `failure` | Amostrar também sucessos, ligar review ao SHA/prompt/modelo e calcular precisão por cenário |
| N10 | P1 arquitetura | Seis componentes fortemente conectados por ciclos de importação | AST do pacote `app` | Romper ciclos por contratos/ports, começando por DB/identity/persona e sales/catalog |
| N11 | P2 | Configuração está centralizada demais e `.env.example` está incompleto | `Settings`: 217 campos; exemplo: 186; 33 aliases ausentes | Dividir settings por domínio, validar perfil no startup e gerar exemplo/documentação do schema |
| N12 | P2 | Logs são mistos e erros amplos são numerosos | 448 `print`, 358 handlers amplos; 635 emissões entre logs estruturados e prints | Migrar para logger JSON único; classificar erro recuperável, terminal e degradado |
| N13 | P2 | Cobertura de testes não é medida | 1.880 casos coletados, mas `coverage`/`pytest-cov` ausente | Adicionar cobertura de branch nos módulos críticos e limiar incremental |
| N14 | P2 | Dependências não são integralmente reproduzíveis | Produção do agente fixa diretas; dev usa ranges; adaptador usa apenas ranges e inclui pytest em runtime | Gerar lock com hashes e separar dependências runtime/dev do adaptador |
| N15 | P2 | Health do TRAYadaptor não prova o commit implantado | Marcador textual fixo `quota-webhook-2026-09-06`, HEAD local `673c9f2` | Expor `GIT_SHA`, data de build e schema version automaticamente |
| N16 | P2 | README principal está defasado após extração modular | Aponta vários módulos antigos no root e descreve defaults divergentes | Atualizar mapa operacional a partir da árvore atual e dos settings |

## Implantação, CI e jobs

O workflow de CI executa scanner, package dry-run, testes unitários e evals offline em pull requests e pushes para `main`. Isso é uma base adequada, mas o commit de produção `bd7be8f` só apresenta o check “Vercel Preview Comments”; não há execução do workflow `CI` nesse SHA. A validação integral existe localmente, porém não está registrada como gate remoto do artefato implantado.

Os últimos pushes de `main` tiveram CI verde. O problema é de fluxo: três merge commits foram acumulados em `main`, novas correções continuaram no branch, a produção foi promovida manualmente desse branch e o PR deixou de existir. A política recomendada é:

1. cada deploy de produção deve expor `git_sha`, `agent_version`, `persona_version`, `prompt_hash` e migration/schema version;
2. produção só pode promover um SHA com CI e evals obrigatórios;
3. o branch de produção deve ser `main`, salvo rollback temporário registrado;
4. o smoke pós-deploy deve bloquear a promoção se health, Tray, metadata, prompt compilation ou replay canário falhar;
5. rollback deve apontar para um SHA conhecido, não para uma string manual de versão.

O workflow `Attendance Learning` promete execução a cada 15 minutos. As execuções públicas recentes falharam em cerca de três segundos no passo `Run attendance learning cron`; o detalhe do log exige autenticação no GitHub, então a causa exata não foi observada. Pelo ponto de falha, as hipóteses são segredo ausente/incorreto, URL incorreta, autorização rejeitada ou resposta não-2xx. Essas hipóteses precisam ser verificadas no log autenticado; não devem ser tratadas como conclusão. A única execução persistida no cursor ocorreu às 02:16 BRT, horário compatível com o cron diário da Vercel, e varreu zero linhas. Isso indica que o fallback diário também não alimentou o loop naquele ciclo; os logs autenticados devem confirmar qual scheduler originou a chamada.

## Revisão da pasta `app`

O pacote contém **253 arquivos, 79.522 linhas, 2.022 funções e 176 classes**. Há 358 handlers `except Exception`/bare e 448 chamadas diretas a `print`. A quantidade não é um defeito isoladamente, mas mostra que a confiabilidade depende de disciplina transversal.

| Módulo | Arquivos / linhas | Avaliação |
|---|---:|---|
| `catalog` | 50 / 12.108 | É o maior domínio e tem boa separação recente entre specs, retrieval, media e vision. Pools internos, hard filters, revalidação e diversidade são sólidos. Prioridade: reduzir ciclos e tornar proveniência/freshness obrigatória por campo. |
| `sales` | 29 / 10.438 | Concentra interpretação, contrato do turno, discovery, council, purchase e resposta. Resolve o problema do produto, mas há regras equivalentes em muitos pontos. Consolidar transições em um estado imutável por turno. |
| `commerce` | 17 / 9.398 | Carrinho, pedido, pagamento, PIX e contexto têm fortes garantias novas. `cart_service.py` ainda possui funções muito longas e efeitos múltiplos; dividir comando, validação e persistência. |
| `stories` | 16 / 6.896 | Cobertura rica para mídia, match e retenção. O fluxo continua muito grande e com muitos fallbacks; separar identificação visual de autorização comercial. |
| `llm` | 17 / 5.730 | Gateway próprio, roteamento Chat/Responses, compiler, presenter e contratos. A abordagem dá controle, mas replica recursos de tracing/session de runtimes maduros. Medir chamada, tokens, custo e motivo de fallback de forma uniforme. |
| `verify` | 9 / 4.445 | Guardrails, factual validator, fact authority, council auxiliar e critique são uma defesa forte. O risco é duplicação de juízes sobre a mesma evidência e bloqueio excessivo. Medir precisão/recall de cada barreira. |
| `memory` | 14 / 4.324 | Boa distinção entre proposals, contact memory, summary e resume. O caso `recipient` mostra que escopo temporal ainda está incorreto. Adotar schema explícito de duração e sujeito do fato. |
| `core` | 6 / 3.256 | Config, DB, security, modelos e mídia centralizam infraestrutura. `db.ensure_tables` tem cerca de 705 linhas e duplica migrations SQL; mover bootstrap para migrations versionadas. |
| `ops` | 13 / 3.063 | Runtime, métricas, rollout, lock e takeover são úteis. Contadores em processo não agregam múltiplas instâncias; métricas de produção devem ir para backend central. |
| `channels` | 9 / 2.548 | Adaptação Brevo/Meta/áudio está bem isolada. Manter envelope aceito imutável e idempotency key do provedor até o recibo. |
| `persona` | 10 / 2.532 | Persona versionada e runtime estão bem desenhados. Ainda precisa ligar versão ativa, prompt hash e resposta no registro de cada turno. |
| raiz de `app` | 9 / 2.403 | Fachadas de compatibilidade reduzem quebra de imports, mas prolongam arquitetura dupla. Planejar remoção por versão e atualizar README/testes. |
| `agents` | 10 / 2.402 | Door/commerce modularizam orquestração. O SCC de cinco módulos `door*` indica dependência circular e dificulta raciocínio sobre ordem de gates. |
| `learning` | 10 / 2.379 | Contratos novos impedem avanço silencioso de cursor. Operacionalmente não está rodando. Primeiro tornar a coleta confiável; depois avaliar promoção automática. |
| `tray` | 8 / 2.281 | Cliente, tools, circuit breaker e probe têm boas fronteiras. Circuit breaker é em memória e se fragmenta por instância; propagar trace e agregar falhas no DB/telemetria. |
| `identity` | 8 / 2.029 | Identidade multicanal e repositório foram endurecidos. Continuar removendo equivalência ambígua e exigir principal/tenant nas operações sensíveis. |
| `http` | 11 / 1.909 | Extração do antigo `api/index.py` foi uma melhoria grande. `brevo_webhook.py` ainda tem handler de cerca de 791 linhas; dividir claim, geração, entrega e commit de auditoria. |
| `ingress` | 7 / 1.381 | Inbox/outbox, lease e serialização estão bem testados. O caminho completo está desativado e seus crons são diários; falta operação contínua antes de ativar. |

O AST encontrou seis componentes fortemente conectados por importações. Os maiores envolvem `core.db`, identidade, persona, commerce e discovery; retrieval/vision/stories; e `sales_agent`, catalog e commerce agents. Esses ciclos não provam falha em runtime, mas tornam monkeypatches, imports tardios e fallbacks amplos necessários. A redução deve ser incremental por interfaces pequenas, sem reescrever todo o agente.

Os maiores pontos de concentração são `core/db.py::ensure_tables` (~705 linhas), `stories/instagram_story_service.py::resolve_story_product_question` (~744), `stories/story_product_matcher.py::match_story_to_catalog` (~605), `http/brevo_webhook.py` (~791 no handler principal), `message_pipeline.py` (~579 no pipeline) e `commerce/cart_service.py` (~398 em criação de checkout). Cada função combina decisões e efeitos; extrair etapas puras permitiria testes de propriedades e reduziria necessidade de `except Exception`.

## Arquivos essenciais fora de `app`

### `api/index.py`

Agora é uma entrada fina que cria o FastAPI e reexporta símbolos para compatibilidade dos testes. Essa redução de mais de duas mil linhas para poucas dezenas é positiva. Os reexports devem ser removidos gradualmente para evitar que os testes preservem acoplamento à arquitetura antiga.

### `vercel.json`

Centraliza rota catch-all e dez crons. Todos rodam uma vez por dia. Isso é adequado para retenção e alguns índices, mas insuficiente para inbox/outbox e aprendizagem contínua. Jobs de entrega precisam frequência de minutos ou processamento contínuo, jitter, lease e alerta de atraso.

### `.github/workflows/ci.yml`

Executa bons gates básicos, mas não mede cobertura, não testa instalação do pacote em ambiente limpo após o dry-run e não executa smoke contra preview. O uso de `-x` reduz tempo, mas esconde a extensão total de uma regressão em cada execução. Manter `-x` no PR rápido e executar suíte completa sem `-x` no merge é uma opção prática.

### `.github/workflows/attendance-learning.yml`

O contrato do shell é razoável: falha se segredos faltarem, usa `curl -fsS` e verifica `ok`. O estado público mostra que esse contrato está detectando uma falha real. Falta publicar um resumo útil mesmo em erro HTTP, preservar status/body sanitizado e emitir alerta.

### SQL e bootstrap

Há 26 migrations e criação/alteração de tabelas duplicada em `app/core/db.py`. A base consultada possui as estruturas de aprendizagem reparadas e RLS conforme a validação anterior. Manter dois mecanismos de evolução aumenta risco de drift. O startup deve apenas verificar versão do schema; migrations devem ser aplicadas por etapa explícita de deploy.

### Configuração

`Settings` possui 217 campos em uma única classe. `.env.example` contém 186 chaves e omite 33 aliases presentes no código, incluindo council, double check, learning, inbox lease, áudio e parte da persona. Também possui duas chaves sem alias correspondente. Além do risco de configuração divergente, o health detalhado anterior mostrou combinações difíceis de interpretar, como rollout `full` com tráfego Responses em 5% e critique em `shadow` apesar de `enforce_on_commerce=true`.

Dividir configuração em `OpenAISettings`, `IngressSettings`, `CatalogSettings`, `CommerceSettings`, `LearningSettings` e `ChannelSettings` reduz ambiguidade. O perfil final deve ser validado no startup com invariantes, por exemplo: async ingress exige consumidor frequente; auto-activate learning exige scheduler saudável; PIX direto exige URL pública; rollout full exige tráfego compatível.

### Dependências e release

O agente fixa versões diretas de runtime, o que é melhor que ranges amplos, mas não trava transitivas. As dependências de desenvolvimento usam ranges. O TRAYadaptor usa ranges em todas as dependências e inclui pytest no `requirements.txt` de produção. Recomenda-se lock reproduzível com hashes e arquivos separados para runtime/dev. O scanner local passou e o package dry-run selecionou 583 arquivos; isso comprova o filtro atual, não substitui scanner de segredos do provedor nem SBOM.

O README ainda aponta `app/openai_gateway.py`, `app/prompt_compiler.py` e outros caminhos anteriores à modularização. Essa defasagem aumenta erro operacional e deve ser corrigida junto da integração em `main`.

## Integrações

### OpenAI

O gateway suporta Chat Completions, Responses, structured outputs, tool loop, orçamento de chamadas, fallback controlado, áudio e visão. O runtime observado antes da promoção usava `gpt-5.4-mini`, no máximo três chamadas por turno, council ativo e validação factual em enforce. O health público v100 não expõe esses flags, e o token administrativo não estava disponível localmente; por isso os valores atuais devem ser confirmados no painel ou endpoint admin.

O projeto já tem abstrações equivalentes a runner, guardrails e sessão. O OpenAI Agents SDK documenta tracing nativo de workflow, turnos, gerações, ferramentas e guardrails, além de `trace_id`/`group_id` e controle de dados sensíveis.[^4] A recomendação não é migrar imediatamente, mas adotar o mesmo contrato de spans no gateway atual. O SDK também oferece testes determinísticos com modelos roteirizados para execução de ferramentas, retries, sessões e guardrails; isso pode inspirar testes menos dependentes de monkeypatch global.[^5]

Tokens e custo não aparecem nos KPIs de produção consultados, embora `TurnRuntimeContext` aceite contagens. A referência do SDK trata uso por execução como dado de primeira classe.[^8] Persistir `input_tokens`, cached tokens, reasoning tokens, output tokens, modelo, API mode e custo estimado por resposta permitiria decidir se council/critique adicional melhora qualidade o suficiente para justificar latência.

### Brevo

O caminho síncrono verificou entrega em 13/13 respostas recentes. O código atual preserva accepted outbound, idempotência e metadata antes de persistir a resposta. Como a v78 gravou `_agent_context` e `_agent_runtime`, mas não `_agent_metadata`, a primeira mensagem v100 deve comprovar que o novo envelope chegou ao banco. Um alerta deve comparar respostas enviadas com respostas persistidas e metadata completa.

### TRAYadaptor/Render

O serviço está saudável, com duas instâncias configuradas no Blueprint e acesso Tray válido no instante observado. O cliente do agente permite até 50 itens por chamada, e a camada de retrieval controla pools/páginas. O rótulo de build é hardcoded e não prova que o Render executa o HEAD local `673c9f2`.

O `Rndr-Id` veio no health, mas não é capturado pelo agente. O cliente deve enviar `X-Request-ID: <trace_id>` e registrar `Rndr-Id`, status, timeout, tentativa, endpoint lógico e tempo. Logs do Render podem então ser filtrados pelo mesmo identificador usado na resposta do agente.[^2]

O circuit breaker atual é por processo. Com duas instâncias Render e funções Vercel efêmeras, cada processo tem visão diferente. Isso é aceitável como proteção local rápida, mas o estado operacional deve ser agregado externamente para detectar degradação global.

### PostgreSQL

O banco é hoje a melhor fonte de auditoria do produto. Ele contém mensagens, respostas, memória, contexto comercial, filas, prompt audit, reviews, learning e catálogo. Pontos positivos: chaves idempotentes, índices, RLS de learning verificada e queries de cursor incrementais. Pontos a melhorar: pool/conexões, schema versionado, tenant obrigatório nas entidades centrais e atomicidade entre estado final, resposta aceita e envio.

### Mercado Pago, PIX e pedidos

As correções v100 adicionaram identidade financeira, proteção contra PIX não pago, validação antes de settlement, retry elegível e sessão comprovada para pedido. Esses contratos estão bem testados offline. Produção indicava Mercado Pago configurado, porém PIX direto e URL pública desativados. Não houve transação real nesta auditoria; o comportamento financeiro continua dependente de teste controlado em sandbox e reconciliação.

### Meta/Instagram e Stories

O código é amplo e inclui assinatura, normalização, mídia privada, match visual, retenção e admin. O health anterior indicava webhook Meta desativado, então essa integração não possui evidência operacional atual. O módulo deve permanecer fora do caminho crítico do WhatsApp e ganhar canário próprio quando ativado.

## Observabilidade e metodologia de avaliação

`app/ops/observability.py` já faz redaction de telefone, email, CPF/CNPJ, cartão e tokens, emite JSON por evento e inclui `trace_id`/`inbound_id`. O middleware devolve `X-Trace-ID`. Essa base é boa. O problema é a adoção parcial: existem 448 prints diretos e muitos handlers amplos que usam formatos diferentes.

OWASP recomenda um identificador de interação que ligue todos os eventos relevantes, registro consistente de quando/onde/quem/o quê e proteção contra inclusão de tokens, senhas e PII desnecessária.[^3] Para este projeto, um turno deveria ter:

```text
trace_id
  ├─ vercel_request_id / deployment_sha
  ├─ inbound_id / provider_message_id
  ├─ prompt_hash / persona_version / model / token_usage
  ├─ tray_call[] / render_rndr_id / result_count / latency
  ├─ guardrail[] / decision / evidence_ids
  ├─ accepted_outbound_id / provider_send_receipt
  └─ response_id / review_id / learning_case_id
```

O conteúdo sensível deve ficar fora dos logs de plataforma. Texto integral, quando necessário para auditoria autorizada, deve permanecer no banco com retenção e acesso controlados. A Vercel limita linhas por request e retenção conforme o plano; depender somente do stdout perde histórico.[^1] Um drain ou tabela de eventos sanitizados é necessário para séries de longo prazo.

KPIs recomendados por versão e cenário:

- taxa de resposta enviada e persistida;
- P50/P95 por etapa e total;
- precisão do retrieval: encontrou elegível quando existia;
- constraint adherence por marca, preço, cor e disponibilidade;
- diversidade possível versus diversidade entregue;
- falso bloqueio por council/validator/critique;
- contexto preservado em follow-up;
- taxa de fallback e chamadas LLM por turno;
- metadata/prompt audit completas;
- backlog/idade de inbox e outbox;
- review coverage, taxa de sucesso e regressão após insight.

## Testes e backtests

A coleta atual encontrou 1.880 testes. O resultado integral já executado no mesmo SHA foi **1.879 passed, 1 deselected**. A distribuição dos casos mostra boa cobertura por volume: commerce 347, sales 230, catalog 230, LLM 125, verify 106, memory 93, stories 86 e channels 83. Há testes específicos para os defeitos da auditoria anterior e para as correções novas.

O que os testes demonstram:

- restrições cumulativas, diversidade e revalidação de catálogo;
- recomposição do council sem inventar catálogo vazio;
- serialização por conversa, accepted outbound e envelope imutável;
- proteção de PIX/pedido e retries;
- memória, resumo, brand unlock e orçamento;
- hardening HTTP, mídia remota e webhooks;
- backtests offline derivados de conversas reais.

O que ainda não demonstram:

- taxa de acerto do modelo real na v100;
- comportamento com latência, 429, timeout parcial e respostas reais de provedores;
- renovação do token Tray após expiração;
- continuidade entre duas funções Vercel concorrentes e duas instâncias Render;
- cobertura de linhas/branches;
- canário pós-deploy e rollback automático por KPI.

O OpenAI Agents SDK separa testes determinísticos do runtime e testes das integrações externas.[^5] O projeto deve manter os 1.880 testes rápidos e acrescentar três camadas:

1. **contract tests gravados** dos payloads reais Brevo/Tray/MP, com segredos removidos;
2. **fault injection** para timeout após aceite, 429, conexão interrompida, resposta parcial, lock expirado e DB indisponível;
3. **canário online pequeno** com orçamento controlado e cenários fixos, comparado por versão.

Adicionar `pytest-cov` com branch coverage ajudará a localizar áreas sem exercício. O objetivo deve ser cobertura incremental dos fluxos críticos, não um número global artificial.

## Comparação com agentes abertos e maduros

O desenho atual deve ser comparado por padrões, sem importar um framework inteiro apenas por popularidade:

- **OpenAI Agents SDK:** runner explícito, sessões, guardrails por fronteira, tracing hierárquico e usage por run.[^4][^8] O NewStoreAgent já possui equivalentes, mas precisa unificá-los em um contrato de execução observável.
- **LangGraph:** distingue checkpoint de thread de store de longo prazo e serializa uma execução por thread no runtime durável.[^6] Essa separação é diretamente aplicável ao defeito `recipient`: nome confirmado do contato pertence ao store; destinatário e apelidos ocasionais pertencem ao thread/checkpoint.
- **Runtimes com execução durável:** API e workers separados, fila com lease e checkpoints reduzem repetição após interrupção.[^7] O projeto já implementa inbox/outbox/lease, mas ainda opera o caminho síncrono e não possui consumidor frequente.

A conclusão arquitetural é preservar os contratos específicos de comércio e usar esses padrões para simplificar infraestrutura. O diferencial do produto está na autoridade factual, regras Tray, checkout e conversação de vendas; tracing, sessões e execução durável devem seguir padrões conhecidos.

## Plano recomendado

### Nas próximas horas

1. Integrar `fix/close-inspected-watch` em `main`, resolver a divergência, executar CI no SHA final e promover esse SHA.
2. Corrigir o workflow `Attendance Learning`; registrar status/body sanitizado e validar uma execução que avance o cursor.
3. Fazer um canário controlado da conversa do anexo:
   - “quero um relógio” → “2500”;
   - “outras sugestões, uma de cada marca nessa faixa”;
   - “mas já tinha encontrado”.
4. Verificar no banco, para cada turno: `_agent_metadata`, `_agent_runtime`, prompt compilation, lista consultada, marcas elegíveis, safety reason e recibo de envio.
5. Repetir após a expiração do token Tray para comprovar o refresh.

### Nesta semana

1. Separar `preferred_name` de `recipient/gift_recipient` e limpar memórias antigas semanticamente inválidas após revisão.
2. Propagar `trace_id` ao TRAYadaptor e persistir IDs Vercel/Render/Brevo/OpenAI.
3. Criar painel por SHA com entrega, latência, retrieval miss, council block, factual failure, metadata e backlog.
4. Tornar o scheduler de aprendizagem único e confiável; amostrar sucessos e falhas.
5. Adicionar lock de dependências, coverage e smoke de preview.
6. Atualizar README e gerar `.env.example` a partir do schema de settings.

### Evolução estrutural

1. Extrair interfaces de DB, catálogo, identidade e persona para romper os maiores ciclos.
2. Transformar o turno em objeto imutável com versão de estado, fatos, evidências, decisão e outbound aceito.
3. Migrar handlers longos para etapas pequenas: normalize → claim → load state → decide → retrieve/act → validate → accept → send → persist/review.
4. Tornar inbox/outbox um runtime contínuo antes de habilitar async ingress.
5. Avaliar tracing compatível com OpenTelemetry ou Agents SDK, mantendo redaction e domínio comercial atual.

## Critério de aprovação da v100

A v100 pode ser considerada validada quando todos os itens abaixo forem demonstrados no SHA de `main`:

- CI e evals verdes no commit efetivamente implantado;
- pelo menos 30 turnos canário, incluindo os três cenários de orçamento do histórico;
- nenhuma resposta perdida ou duplicada;
- 100% das respostas com metadata, runtime, prompt hash e recibo;
- diversidade entregue sempre que houver marcas elegíveis, com explicação honesta quando não houver;
- nenhum nome não confirmado atravessando conversas;
- council/factual validator sem falsos “não encontrei” nos casos conhecidos;
- P95 por turno abaixo do SLO definido e spans capazes de atribuir o atraso;
- aprendizagem executando no intervalo previsto, avançando cursor e sem promoção com amostra insuficiente;
- refresh Tray comprovado após expiração;
- rollback testado para SHA anterior conhecido.

## Fontes

[^1]: Vercel, [Runtime Logs](https://vercel.com/docs/logs/runtime) e [Vercel CLI logs](https://vercel.com/docs/cli/logs). Consultado em 08/09/2026.
[^2]: Render, [Logs in the Render Dashboard](https://render.com/docs/logging). Consultado em 08/09/2026.
[^3]: OWASP Cheat Sheet Series, [Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html). Consultado em 08/09/2026.
[^4]: OpenAI Agents SDK, [Running agents](https://openai.github.io/openai-agents-python/running_agents/) e [Tracing](https://openai.github.io/openai-agents-python/tracing/). Consultado em 08/09/2026.
[^5]: OpenAI Agents SDK, [Testing](https://openai.github.io/openai-agents-python/testing/). Consultado em 08/09/2026.
[^6]: LangGraph, [Persistence](https://langchain-ai.github.io/langgraph/concepts/time-travel/). Consultado em 08/09/2026.
[^7]: LangGraph, [Agent Server runtime and durable task queue](https://langchain-ai.github.io/langgraph/tutorials/langgraph-platform/local-server/). Consultado em 08/09/2026.
[^8]: OpenAI Agents SDK, [Usage](https://openai.github.io/openai-agents-python/usage/). Consultado em 08/09/2026.

### Evidências locais e privadas

- `docs/audits/2026-09-08/pytest-final.txt` e `pytest-final.xml` — suíte integral no SHA auditado.
- `docs/agent_audit_remediation_2026-09-08.md` — relação entre achados anteriores e correções.
- `app/catalog/retrieval/limits.py` e `availability.py` — pools, limite de resposta e diversidade.
- `app/http/brevo_webhook.py`, `app/core/db.py` e `app/ops/observability.py` — entrega, persistência e tracing.
- `app/memory/contact_preference_memory.py` e `app/sales/qualification_slots.py` — persistência e interpretação de `recipient`.
- `.github/workflows/ci.yml`, `.github/workflows/attendance-learning.yml` e `vercel.json` — gates e agendas.
- Banco PostgreSQL de produção — consultas read-only executadas em 08/09/2026; nenhum identificador pessoal foi incluído neste relatório.
