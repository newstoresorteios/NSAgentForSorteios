# Plano de implementação: qualidade e custo do NSAgent

Data: 23/09/2026. Status: controles e executores P0–P7 implementados e testados localmente; validação paga, metas de qualidade e ativação em produção ainda não aprovadas. Consulte o fechamento abaixo; os blocos anteriores são históricos.

## Objetivo

Melhorar continuidade da conversa, precisão da consulta e conclusão da compra, preservando a experiência atual que funciona e reduzindo custo e latência. A implementação deve tratar classes de falha, sem regras específicas para telefone, pessoa, marca ou relógio.

Referência: análise técnica em docs/analise-arquitetura-agentes-modelos-2026-09-23.md.

## Acesso à documentação: concluído

- OpenAI Developers: instalado e habilitado, conforme consulta ao diretório de plugins.
- Cadastro local mcp_openai corrigido: antes executava o próprio comando de cadastro como servidor stdio; agora utiliza streamable_http em https://developers.openai.com/mcp.
- Verificado com codex mcp get mcp_openai: enabled=true e transporte/URL corretos.
- Verificação direta do protocolo: initialize respondeu como openai-docs-mcp 1.0.0; tools/list retornou ferramentas; search_openai_docs trouxe resultados; fetch_openai_doc retornou a documentação de orquestração.
- O inventário de ferramentas desta conversa ainda não contém as ferramentas do novo servidor. Configuração e acesso HTTP estão validados; carregamento nativo na sessão não está comprovado. Verificar em uma nova sessão do Codex quando necessário.
- O MCP é público e de documentação. Esse teste não valida credenciais, saldo ou modelos acessíveis na API de inferência. Nenhuma chave foi criada ou alterada.

Fonte: [Docs MCP](https://developers.openai.com/learn/docs-mcp).

## Regras para todas as fases

- Persona, mensagens, perguntas, instruções comerciais, seleção de modelos e limites operacionais ficam no banco e na configuração avançada.
- Código mantém contratos, validação, isolamento de workspace e integridade das ações.
- Preservar a preferência: transferência humana somente após pedido ou aceite explícito.
- Um modelo sugerido por padrão, sem marcador numeral e sem asteriscos nas respostas.
- Perguntas naturais geradas a partir da persona e do contexto; contingências editáveis no banco.
- Preservar informações válidas do cliente ao trocar de produto. Não reutilizar cotação, variante ou carrinho incompatíveis.
- Registros históricos usados em testes devem ser minimizados e isolados de envios reais.
- Nenhuma regressão paga recorrente será ativada por este plano.
- Executar etapas em sequência, com flags inicialmente desligadas quando houver mudança de comportamento.
- Cada fase produz alteração revisável, evidências de teste, migração quando necessária e caminho de reversão.

## P0 — Congelar a referência e critérios de aceitação

### Entregas

- Separar e registrar as correções locais já prontas: saudação com pedido encerrado e candidato técnico sem preço.
- Registrar commit, versão da persona, configuração, modelos e conjunto de testes de referência.
- Criar conjunto inicial de 20–30 conversas completas com resultados esperados revisáveis.
- Separar os casos de ajuste dos casos reservados para validação; ampliar a validação para cumprir os critérios existentes antes de alegar maturidade acima de 90%.
- Classificar falhas por interpretação, recuperação, confirmação, composição, estado ou integração.

### Casos obrigatórios

- Saudação após pedido enviado, sem bloquear uma compra nova.
- Automático + safira + orçamento; ficha sem preço, produto indisponível e característica desconhecida.
- Resposta curta: “2”, “modelo 2”, “aço”, “esse”, “manda”, “qual o próximo passo?”.
- Seleção do produto → variante → frete com CEP já informado → carrinho → checkout.
- Troca explícita de produto e retomada posterior, com dados pessoais preservados.
- Fotos parecidas, recorte da mesma imagem e impossibilidade de confirmar referência.
- Mensagens agrupadas e mensagens novas durante o processamento.
- Timeout, catálogo vazio, API indisponível, reentrega do webhook e envio duplicado.

### Aceitação

Resultados esperados incluem produto/variante, restrições, chamadas permitidas/proibidas e estado final. Não depender apenas de frases exatas nem de nota de um juiz LLM.

## P1 — Uma única autoridade para evidência comercial

### Alterações

- Introduzir contrato tipado de resultado de consulta, com versão de schema.
- Separar approved_products, rejected_candidates, missing_evidence e integration_errors.
- Cada fato traz origem, horário, produto/variante e status confirmado, desconhecido ou divergente.
- Produtos recomendáveis exigem identidade, preço válido, disponibilidade e todos os requisitos obrigatórios confirmados.
- Consulta explícita sobre produto conhecido pode explicar falta de preço/estoque sem promover o item como recomendação.
- Composição recebe somente fatos aprovados; auditoria mantém os candidatos rejeitados.
- Toda busca adicional do revisor passa pelo mesmo contrato e pelos mesmos filtros.
- Ajustar adaptadores temporários para os consumidores atuais de commercial_data e response_metadata.

### Arquivos principais

app/catalog/retrieval/technical.py, hard_filter.py, availability.py; app/sales/responder.py; app/verify/response_critique.py, final_response.py; app/core/models.py.

### Aceitação

Nenhum candidato rejeitado reaparece como oferta ou alvo de compra. Validações gerais funcionam com diferentes produtos e combinações de características. A consulta de um item conhecido continua respondendo sobre esse item.

## P2 — Estado consistente e persistência por turno

### Alterações

- Consolidar as transições de seleção, refinamento, troca de assunto, carrinho e pedido encerrado.
- Diferenciar produto mencionado, recomendado, escolhido e inserido no carrinho.
- Separar preferências da busca, seleção atual, dados do cliente e histórico de pedidos.
- Modelar uma pergunta pendente com assunto e opções válidas: “aço” deve completar a variante solicitada.
- Persistir estado a partir do resultado final validado; evitar gravações concorrentes por módulos de revisão.
- Integrar revisão de versão, idempotência e agrupamento já existentes; mapear lacunas antes de acrescentar controles.
- Manter registro de entrega para não tratar mensagem que falhou no envio como oferta efetivamente apresentada.
- Carrinho abandonado pode alimentar remarketing, mas não deve prender a próxima busca.

### Aceitação

Reproduzir sequências completas e concorrência controlada. Não perder CEP, trocar SKU silenciosamente, duplicar carrinho nem reutilizar variante incompatível. Pedido antigo permanece consultável no pós-venda.

## P3 — Contexto enxuto, persona e mensagens editáveis

### Alterações

- Medir tokens das camadas atuais antes de removê-las ou compactá-las.
- Definir contextos específicos para interpretação, consulta, composição e revisão.
- Eliminar representações redundantes de estado e enviar somente o histórico pertinente.
- Recuperar políticas institucionais por assunto, com fonte e versão.
- Preservar dados estruturados críticos fora de resumos livres.
- Colocar instruções estáveis antes de dados dinâmicos e validar estratégia de cache para o modelo utilizado.
- Inventariar e migrar mensagens/instruções comerciais ainda fixas para agent_configuration_catalog.
- Revisar as perguntas de descoberta: perguntar quando a resposta pode reduzir ambiguidade relevante; consultar quando já houver critérios suficientes; evitar repetir informação conhecida.

### Arquivos principais

app/llm/prompt_compiler.py; app/sales/responder.py; app/memory; app/sales/adaptive_discovery.py; sql/seeds/operator_catalog.json e migrações.

### Aceitação

Redução mensurável de tokens sem perder fatos obrigatórios nem piorar a aprovação dos cenários. Meta inicial proposta: redução de pelo menos 30% da entrada mediana no conjunto comparável; ajustar somente com justificativa registrada.

Referências: [latência](https://developers.openai.com/api/docs/guides/latency-optimization), [cache](https://developers.openai.com/api/docs/guides/prompt-caching).

## P4 — Política de modelos por função

### Alterações

- Centralizar seleção por interpretação, composição, visão, revisão e avaliação.
- Configurar modelo, esforço, limite de saída, timeout e fallback por função no banco.
- Validar capacidades por modelo: Responses, saída estruturada, ferramentas e parâmetros aceitos.
- Atualizar a detecção de famílias e permitir esforços compatíveis, incluindo none quando suportado.
- Validar fallback de transporte: uma alternativa de API não pode receber parâmetros incompatíveis.
- Registrar modelo solicitado/efetivo, esforço, tokens, cache, duração e motivo de escalonamento.
- Acrescentar limites por turno e por campanha em chamadas, tokens e custo estimado, considerando preços versionados.
- Novos campos devem aparecer no catálogo consumido pela configuração avançada; validar exibição, edição e publicação no front antes da liberação.

### Aceitação

Configuração inválida é rejeitada antes da chamada. Alterar uma função não altera silenciosamente as demais. Falha de integração não dispara repetição ilimitada. Preservar gpt-5.4-mini como baseline até concluir P6.

## P5 — Busca visual com confirmação dos candidatos

### Alterações

- Manter recuperação inicial por descritor e filtros, registrando sua incerteza.
- Comparar a imagem recebida com imagens dos poucos candidatos selecionados, com limite configurável.
- Retornar evidências e diferenças estruturadas; não usar apenas confiança autodeclarada.
- Confirmar atributos técnicos e comerciais na ficha oficial.
- Reutilizar resultado de imagem equivalente quando seguro, invalidando-o quando a foto ou o contexto relevante mudar.
- Quando não houver confirmação, formular pergunta útil baseada na lacuna encontrada, sem repetir a mesma solicitação após novo recorte.

### Aceitação

Testar imagens semelhantes, versões GMT/não GMT, pulseiras diferentes e produtos ausentes do catálogo. Nenhuma referência exata afirmada apenas pela aparência; nenhum link construído por suposição.

## P6 — Avaliação comparativa com orçamento

### Execução

- Rodar primeiro validações determinísticas e simulações sem chamadas pagas.
- Comparar o modelo atual otimizado com um candidato por vez. Luna e Sol são candidatos, não escolhas aprovadas.
- Congelar catálogo simulado, persona, configuração e código; depois executar amostra de integração real separada para avaliar atualização dos dados.
- Usar juiz com critérios explícitos, checagens objetivas e revisão humana de amostra. O avaliador não pode aprovar uma oferta inválida por estar bem escrita.
- Definir teto financeiro antes da execução paga; interromper antes de exceder a reserva estimada da próxima chamada e registrar execução como incompleta, sem alegar aprovação.
- Reaproveitar resultados sem recalcular casos idênticos e repetir os casos críticos para medir variação.
- Nunca enviar mensagens, criar pedidos ou movimentar carrinhos de clientes reais no replay.

### Critérios de liberação

- Mais de 90% de aprovação em validação separada, cumprindo cobertura e repetição exigidas pelo score existente.
- Zero erro crítico observado no conjunto de liberação.
- Informar tamanho da amostra e limitações; não transformar o percentual em garantia universal.
- Comparar custo por conversa concluída, tokens, chamadas, mediana e p95 de latência.
- Nenhuma regressão nos fluxos existentes de compra, pós-venda, consentimento de transferência e configuração.
- Rejeitar candidato que melhore apenas o texto, mas piore produto, estado ou custo além do limite.

Referência: [avaliação de agentes](https://developers.openai.com/api/docs/guides/agent-evals).

## P7 — Piloto de Agents SDK e auditor de qualidade

### Piloto SDK

- Encapsular uma capacidade delimitada de leitura, mantendo a interface com o orquestrador atual.
- Manter Crono responsável pela resposta ao cliente; especialistas retornam resultados estruturados.
- Não adicionar chamadas LLM a verificações que o código já executa corretamente.
- Ativar por flag somente após medir equivalência funcional e ganho operacional.

### Auditor

- Reutilizar a infraestrutura de replay e avaliação existente.
- Entrada: conversa minimizada, configuração utilizada, consultas, evidências, decisões e resultado final.
- Saída: classificação da falha, trecho de evidência, causa provável, teste proposto e recomendação revisável.
- Não mudar prompt, persona ou código de produção automaticamente com base no julgamento do auditor.
- Começar por execução manual em lote com orçamento. Agendamento depende de decisão posterior explícita.
- Agents API permanece opção para essa tarefa longa, após comparar com a execução já disponível. Se adotada, mapear sessões, renovação de instruções e custos.

Referência: [orquestração e especialistas](https://developers.openai.com/api/docs/guides/agents/orchestration).

## Liberação e reversão

- A ordem é P0 → P1 → P2 → P3 → P4 → P5 → P6 → P7.
- Entregar em mudanças pequenas; não juntar troca de modelo, refatoração de estado e mudança de prompt na mesma comparação.
- Migrações aditivas e compatibilidade temporária; não apagar colunas ou configurações necessárias ao rollback.
- Habilitação gradual com conjunto controlado e sem personalização permanente por contato.
- Reversão restaura a versão anterior da configuração e a flag do fluxo. Não restaurar cegamente estados antigos sobre carrinhos novos.
- Após cada liberação, conferir respostas, estado final, erros de integração, custo e latência. Investigar regressões antes da próxima fase.
- Push e implantação devem ser registrados separadamente de implementação e testes; uma alteração local não equivale a produção corrigida.

## Uso da documentação no desenvolvimento

Antes de implementar mudanças de API, SDK ou modelo, consultar e ler a página atual pelo MCP oficial. Registrar a fonte na alteração quando afetar compatibilidade. Se o MCP não estiver carregado na sessão, consultar diretamente os domínios oficiais e registrar a limitação. Documentação não substitui testes no NSAgent.

Os nomes de contratos e campos propostos neste plano são desenho de implementação, não recursos já presentes. A lista final de arquivos e migrações será refinada ao iniciar cada fase.


## Verificação adicional: conhecimento institucional

Solicitada após relato de pergunta sobre política de troca receber oferta de atendimento humano.

- Confirmar no banco os documentos de troca/devolução, frete, FAQ, pagamento e garantia: publicados, vigentes e vinculados ao workspace/persona corretos.
- Rastrear a pergunta “Qual é a política de troca dos relógios?”: classificação, consulta institucional, trechos recuperados, composição e revisão final.
- Distinguir ausência de documento, falha de recuperação e descarte da informação durante revisão.
- Reproduzir offline a conversa com saudação seguida da pergunta; garantir resposta fundamentada na política e transferência apenas após pedido/aceite.
- Registrar documentos/versões efetivamente consultados; cadastro por si só não comprova uso.
- Verificar separadamente em produção com logs, sem disparar regressão paga automática.

Status: confirmado no banco em 23/09/2026. Sete documentos publicados. A mensagem 883 foi interceptada como trade_in_or_appraisal antes de consultar a base, sem chamada ao modelo. Corrigido o roteamento e adicionada evidência de recuperação à resposta.


## Primeiro bloco implementado em 23/09/2026

- Contrato tipado por turno para o resultado da busca técnica, incluindo produtos aprovados e resposta de contingência oriunda do catálogo de mensagens.
- Validação final impede que revisões reincluam candidatos rejeitados ou alterem fatos comerciais sem nova validação. Alterações nos fatos invalidam a oferta; não restauram preços antigos.
- Evidência completa permanece para auditoria; o redator recebe apenas evidência dos produtos aprovados.
- Removida numeração na apresentação técnica.
- Regressões locais cobrem alteração de preço, disponibilidade, identidade, características, recuperação indevida de rejeitados e continuidade independente entre turnos.
- Escopo inicial: busca técnica. Expansão do contrato para descoberta adaptativa, imagem e checkout continua pendente; regras de perguntas existentes foram preservadas.
- Vercel autenticada; presença de OPENAI_API_KEY confirmada sem baixar a chave. Nenhuma avaliação paga executada neste bloco.
- A consulta MCP à documentação de Structured Outputs confirmou o uso de contratos tipados; validação estrutural não substitui validação comercial.

Validação local do primeiro bloco: 2315 testes passaram, 6 ignorados, 8 avisos de depreciação SQLite; 844 testes adicionais passaram com descoberta contextual e adaptativa ativadas. Sem chamadas pagas de inferência.


## Implementação das prioridades — 23/09/2026

| Etapa | Entrega e limites de validação |
| --- | --- |
| P0 | 25 cenários congelados, 29 passos, divisão desenvolvimento/validação e manifesto de referência em evals. O subconjunto ainda não satisfaz os critérios completos de liberação. |
| P1 | Contrato de oferta separa produtos aprovados, rejeitados, evidências ausentes e erros de integração. Revisores não podem reinserir candidatos sem validação. Extensão genérica protegida por approvedOfferContractEnabled. |
| P2 | Estados mencionado/recomendado/escolhido/no carrinho; revisão não grava seleção. CEP e estado anterior preservados na resposta institucional. Testes de continuidade existentes mantidos. |
| P3 | Recuperação institucional antes da interpretação comercial, fontes e hash no trace. Compactação sem perda por referências, desligada por padrão. Instruções e mensagens cadastradas no banco. |
| P4 | Modelo, raciocínio, limite de saída, timeout e fallback por função, com validação de capacidades. Modelo de produção preservado. Backend valida configurações antes da publicação. |
| P5 | Comparações visuais auditáveis e cache por turno/conteúdo. Fotos idênticas entre candidatos não comprovam SKU. Ambiguidade preservada até a resposta final. |
| P6 | Comparador de amostras equivalentes e reserva atômica de orçamento no banco. Ausência de métricas não vira custo zero. Campanha paga desativada; comparação real pendente. |
| P7 | Piloto isolado do Agents SDK com consulta institucional somente leitura, executado com modelo simulado. Auditor determinístico manual propõe testes revisáveis. Avaliação semântica com modelo e piloto real pendentes. |

### Confirmação do conhecimento institucional

Os sete registros estão em business.institutional_knowledge no bundle publicado, e não em anexos da persona. A pergunta sobre política de troca (inbound 883, 23/09/2026 às 12:16 UTC) recebeu safety_reason trade_in_or_appraisal, sem consultas Tray ou OpenAI. O teste de reprodução agora verifica a passagem do documento de troca ao redator, a conservação da resposta após revisão e a ausência de transferência automática. Isso comprova o fluxo local simulado; a confirmação do novo comportamento em produção depende da implantação.

### Operação, custos e implantação

A migração 20260923165605_agent_quality_priorities foi aplicada ao banco: catálogo de controles e tabela de reserva de orçamento com RLS. Nenhum carrinho, pedido ou conversa de cliente foi alterado. O teste transacional de reserva foi revertido. evaluationCampaignPolicy.enabled e agentsSdkPilotEnabled permanecem falsos; não foi criado agendamento. Nenhuma inferência paga foi executada nesta implementação.

approvedOfferContractEnabled e compactRoleContextEnabled permanecem falsos para comparação controlada. institutionalDirectAnswerEnabled está habilitado na configuração, mas requer implantação do código novo. modelRolePolicies vazio preserva o modelo atual gpt-5.4-mini. Novos campos usam os editores genéricos da configuração avançada; o backend complementar inclui validação de JSON, regex, capacidades e orçamento. Implantar esse backend antes de operadores configurarem políticas por função.

O SDK 0.22.3 exige OpenAI 3.x, incompatível com a versão fixada no runtime principal. Por isso requirements-sdk-pilot.txt e ambiente separado; nenhuma troca de dependência do serviço principal. O piloto não está ligado à resposta de clientes. O controle de orçamento foi conectado ao provedor SDK no fechamento abaixo. Execução paga continua dependendo de campanha publicada com teto e credencial no ambiente de execução.

Para reverter o fluxo institucional, desligar institutionalDirectAnswerEnabled. As demais expansões já estão desligadas. Não reverter estados comerciais nem apagar o catálogo ou a tabela de orçamento. Push, implantação, teste real e liberação de qualidade são etapas distintas; nenhuma maturidade superior a 90% está certificada por estes testes offline.

### Resultado final dos testes locais

- Suíte principal: 2349 passaram, 7 ignorados, 8 avisos preexistentes de depreciação SQLite.
- Descoberta contextual/adaptativa ativada: 846 passaram.
- Backend complementar, validação/publicação de configuração: 28 passaram.
- Piloto SDK em ambiente isolado com modelo simulado: 1 passou.
- git diff --check sem erros de whitespace; avisos de normalização LF/CRLF.
- Banco: RLS ativa no orçamento; anon sem leitura, authenticated sem escrita, service_role com atualização; nenhuma reserva de teste remanescente.
- Estes grupos têm sobreposição e não devem ser somados como casos distintos de qualidade.


## Fechamento da implementação P0–P7

### P0 — Cobertura

A referência foi ampliada de 25 para 97 cenários, 110 passos e 25 casos de validação. O executor agenda três execuções independentes por cenário crítico. IDs históricos foram herdados do gerador de cenários existente; respostas históricas incorretas não são usadas como resposta esperada. Cobertura definida não significa campanha executada: o score mantém falhas e casos ausentes no denominador.

### P1 e P2 — Oferta e continuidade

A proteção genérica de ofertas foi exercitada com a flag ligada. Fixtures que só simulavam nome/preço foram corrigidas para representar confirmação comercial real, sem relaxar a validação do serviço. Contingência sem critérios técnicos agora usa mensagem própria do banco e não exibe um campo vazio. Testes de memória, variante, checkout, entrega e recebimento preservam os caminhos existentes. Casos antigos que verificam qualificação fixa selecionam explicitamente o modo legado; a descoberta contextual e adaptativa tem testes separados.

### P3 — Medição de contexto

scripts/measure_quality_context.py mede tokens offline com o200k_base. No estado e catálogo congelados, a mediana passou de 3112 para 2588 tokens (16,84%). A serialização compacta e a deduplicação não removem fatos. Esta medição não inclui o prompt completo, histórico ou envelopes do provedor. A meta original de 30% não foi atingida; ela permanece pendente, e não foi reduzida para declarar sucesso. A compactação permanece protegida por flag até comparação generativa.

### P4 — Capacidades e interface

O runtime rejeita o transporte primário incompatível antes da chamada. Quando a função não define seu próprio limite de saída, Chat Completions herda o limite global efetivo, preservando o teto reservado. O front prioriza whenUsed e description publicados no catálogo, sem um mapa local obrigatório para novos campos. Quinze controles receberam documentação no banco. O backend valida os limites de custo e latência da comparação.

### P5 — Ambiguidade visual

Fotos compartilhadas por dois candidatos continuam ambíguas após a revisão final. Uma consulta posterior mais restrita não pode apagar uma ambiguidade já observada. Testes incluem empate visual praticamente idêntico, candidato ausente, imagem de concorrente não carregada, variante semelhante e proteção contra substituição pelo revisor. A confirmação com imagens e catálogo de produção ainda não foi medida nesta etapa.

### P6 — Executor e comparação

scripts/run_controlled_campaign.py usa a persona publicada e somente comércio simulado. Conserva estado/histórico entre passos, isola cenários e repetições, registra versão de código/configuração/persona/fixtures, cria marcador exclusivo por amostra e reutiliza apenas resultados concluídos com identidade idêntica. Uma execução interrompida permanece incompleta e não é reexecutada automaticamente. Orçamento não pode ser sobrescrito pelo arquivo de candidato.

scripts/compare_quality_campaigns.py verifica amostras equivalentes, qualidade, cobertura, custo e p95. Métricas ausentes não equivalem a zero. Tetos max_cost_ratio e max_latency_ratio ficam dentro de evaluationCampaignPolicy.comparison_limits; padrão 1 significa não aceitar aumento. O comparador não promove configurações automaticamente. Chamadas de revisão entram na medição, além do atendimento.

### P7 — SDK e auditor

O provedor do SDK usa seleção explícita pela função evaluation, teto real de saída, timeout, reserva durável antes de cada chamada, zero retries do cliente e tracing externo desativado. Handoffs, contexto remoto e streaming estão fora do piloto delimitado. A fábrica é a via de produção; injeção de Model é o ponto de teste offline. O cliente HTTP é fechado após o piloto.

scripts/run_sdk_pilot.py consulta documentos sem executar ações comerciais. scripts/audit_conversation_report.py --semantic executa auditoria estruturada com evidência, causa provável, teste e recomendação. Falhas objetivas prevalecem sobre aprovação do modelo; orçamento indisponível resulta em inconclusivo. Nenhuma sugestão é aplicada automaticamente.

### Banco, implantação e limite de execução

Aplicadas as migrações 20260923180000_quality_campaign_completion e 20260923183000_quality_comparison_limits. Catálogo e tetos verificados após aplicar. Nenhuma campanha paga ou agendamento foi habilitado. O deploy anterior do NSAgent está Ready na Vercel e o backend b2f3cdb está live no Render. As alterações deste fechamento permanecem locais até novo push/implantação.

A CLI Vercel está autenticada, mas a plataforma não permite exportar os segredos protegidos de produção. O comando env run confirmou OPENAI_API_KEY e DATABASE_URL indisponíveis para execução local. Acesso não foi contornado. Portanto comparação generativa real, auditoria semântica real, piloto real e aprovação >90% não foram executados. Os executores estão prontos para um ambiente com as credenciais existentes e orçamento publicado; nenhuma aprovação de qualidade foi simulada.

### Comandos de operação manual

Executar na raiz do NSAgent, em ambiente com as credenciais existentes. O piloto requer o ambiente isolado de requirements-sdk-pilot.txt. Substituir WORKSPACE e os arquivos de saída; não há execução agendada.

```powershell
python scripts/run_controlled_campaign.py --workspace WORKSPACE --suite evals/priority_reference.json --output evals/results/baseline
python scripts/run_controlled_campaign.py --workspace WORKSPACE --suite evals/priority_reference.json --output evals/results/candidate --overrides candidate.json
python scripts/compare_quality_campaigns.py --baseline baseline-summary.json --candidate candidate-summary.json --output evals/results/comparison.json
python scripts/audit_conversation_report.py --report replay.json --expected expected.json --output evals/results/audit.json --semantic --workspace WORKSPACE
.venv-sdk-pilot/Scripts/python scripts/run_sdk_pilot.py --workspace WORKSPACE --question "Qual é a política de troca?"
```

Campanha sem orçamento publicado falha antes do provedor. Não remover marcadores de execuções interrompidas para tentar de novo: revisar primeiro o consumo e registrar outra identidade de campanha. Nunca editar os resultados para satisfazer os critérios de liberação.

### Verificação final deste fechamento

- NSAgent: 2359 passaram, 7 ignorados, 8 avisos de depreciação SQLite.
- Recorte com proteção de ofertas e compactação ligadas: 1649 passaram, 1 ignorado.
- Descoberta adaptativa/contextual junto dos controles novos, com fixtures apropriadas: 892 passaram.
- Backend de configuração: 33 passaram.
- Front: 10 testes de configuração passaram e build TypeScript/Vite concluído; aviso de bundle acima de 500 kB preexistente.
- SDK isolado: 2 passaram, incluindo bloqueio antes do provedor e limite efetivamente enviado; aviso de escopo do pytest-asyncio.
- Nova medição: zero inferências pagas; banco confirma evaluationCampaignPolicy.enabled=false.
- Os recortes se sobrepõem. Não somar essas contagens como conversas independentes.
