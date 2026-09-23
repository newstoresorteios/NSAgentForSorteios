# NSAgent: arquitetura, agentes e modelos

Análise de 23/09/2026. Escopo: documentação oficial indicada, código local, configuração publicada e telemetria do banco. Esta análise não altera modelos, serviços, persona ou automações. As alterações locais da correção anterior permanecem pendentes de push.

## Conclusão

A melhoria prioritária é consolidar contexto, evidências e responsabilidade pela resposta final. Trocar o modelo pode melhorar interpretação e redação, mas não corrige estados comerciais incorretos, preço ausente ou referências que a consulta não recuperou.

Recomendo manter Responses API, avaliar Agents SDK incrementalmente e experimentar modelos por função. Agents API pode servir primeiro a um auditor separado do atendimento. A decisão de adotar vários agentes deve depender de melhoria demonstrada nos testes, conforme a [orientação de avaliações](https://developers.openai.com/api/docs/guides/evaluation-best-practices).

## Evidências do projeto

### Configuração publicada

Consulta somente de leitura no workspace ativo, configuração versão 6:

| Função/configuração | Valor |
|---|---|
| Modelo padrão | gpt-5.4-mini |
| Modelo principal | gpt-5.4-mini |
| Modelo rápido | gpt-5.4-mini |
| Transporte | Responses API |
| Esforço de raciocínio | medium |
| Modelo específico de visão | Vazio; o código herda o padrão |
| Transcrição | whisper-1 |
| Síntese de voz | gpt-4o-mini-tts |
| Embedding do descritor visual | text-embedding-3-small |
| Limite de chamadas LLM | 3 normais, 4 complexas |
| Crítica | shadow, com promoção para enforce em comércio |
| Crítica por risco | Ativada |
| Amostragem de crítica sem risco | 0 |

Os defaults gpt-4.1-mini/nano existentes no código não representam o modelo publicado. A telemetria confirma gpt-5.4-mini nas 48 chamadas registradas da amostra.

### Amostra operacional

21 respostas com turn_runtime, janela consultada de 22/09 até antes de 24/09 UTC, limitada aos registros disponíveis no momento da consulta. É uma amostra pequena, não um retrato estatístico de todo o atendimento.

| Indicador | Resultado |
|---|---:|
| Chamadas LLM por turno | 2,29 |
| Entrada por turno | 24.309 tokens |
| Saída por turno | 2.485 tokens |
| Processamento médio | 27,82 segundos |
| Percentil 95 de processamento | 61,17 segundos |
| Entrada total | 510.490 tokens |
| Entrada em cache | 47.360 tokens, aproximadamente 9,3% |

As composições com envelope receberam média de 18.913 tokens de entrada; as revisões, 14.755. O contexto merece atenção antes de aumentar a quantidade de especialistas.

No incidente do automático com safira até R$ 2.500, o turno 885 consumiu 35.721 tokens de entrada, 4.101 de saída, três chamadas LLM e 11 chamadas Tray, em 53,84 segundos. A interpretação extraiu corretamente as exigências. O problema ocorreu na seleção/apresentação e no estado, não na compreensão inicial. Esse achado limita o benefício esperado de apenas substituir o modelo.

## O que já está bem encaminhado

- Gateway central com Responses API, saídas estruturadas e telemetria por chamada.
- Persona, instruções e políticas carregadas do banco, com versionamento.
- Memória de conversa e estado comercial explícitos.
- Consulta e revalidação de fatos no catálogo.
- Filtros técnicos, validação final e controle de orçamento de chamadas.
- Histórico de regressão, simulação de ferramentas e critérios objetivos.
- Pontuação com denominador fixo, separação de validação e repetição de casos críticos.

Essas capacidades devem ser preservadas. SDK e API são formas de executar agentes; não substituem as regras de negócio nem garantem respostas corretas. A [comparação oficial dos runtimes](https://developers.openai.com/api/docs/guides/agents) distingue o controle da aplicação, do SDK e do serviço gerenciado.

## Fragilidades observadas

### 1. Autoridade distribuída sobre produto e resposta

Consulta, composição, crítica, fallbacks e finalização podem alterar o resultado. Por exemplo, apply_search_products_to_result também atualiza active_product e last_presented_products. Há proteções existentes, mas muitas transições precisam permanecer sincronizadas.

Proposta: um resultado comercial estruturado, com produtos aprovados, rejeitados, critérios confirmados, critérios desconhecidos, validade da consulta e próxima ação permitida. A composição usa exclusivamente os aprovados; rejeitados permanecem na auditoria. A memória comercial é persistida a partir do resultado final validado.

### 2. Contexto repetido e amplo

O responder envia STATE_FACTS, WORKING_MEMORY, RESPONSE_CONTRACT, plano, histórico, catálogo de capacidades e evidências. O compilador já remove duplicações literais, mas ainda existem representações sobrepostas.

Proposta: pacotes por tarefa. Interpretador recebe mensagem e estado relevante; redator recebe intenção e fatos aprovados; revisor recebe afirmações e evidências correspondentes. Conhecimento institucional entra somente quando relevante. Medir tokens por camada antes e depois.

Manter instruções estáveis antes do conteúdo dinâmico pode favorecer cache. A estratégia precisa considerar as regras do modelo, inclusive cobrança de escrita de cache nas famílias recentes. [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).

### 3. Configuração de modelos por função incompleta

Existe resolve_openai_model(main/fast), mas responder e crítica ainda usam settings.openai_model diretamente. Hoje os três valores são iguais, portanto não há economia efetiva por função.

Proposta: modelos e esforços separados para interpretação, composição, visão, revisão e avaliação. Todos editáveis no banco, com validação de compatibilidade e registro da configuração efetiva por chamada.

### 4. Compatibilidade de novos modelos

model_capabilities reconhece raciocínio por famílias o1/o3/o4/gpt-5. GPT-6 não está nessa lista; sem override, seria tratado como modelo sem controles de raciocínio e com temperatura permitida. O campo openai_reasoning_effort também não aceita none, apesar de esse valor ser documentado para modelos candidatos.

É necessário corrigir esse contrato antes de experimentar a nova família, incluindo transporte de fallback, schemas, limites de saída e parâmetros aceitos. A documentação de [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol) exige atenção especial ao uso de ferramentas no Chat Completions; Responses é o caminho recomendado para essa experiência.

### 5. Busca visual depende de descrições intermediárias

O índice usa embedding textual do fingerprint visual. Isso representa uma descrição extraída da imagem, não uma comparação direta entre pixels. Se o descritor perde detalhes, modelos próximos podem ficar semelhantes.

Proposta: recuperar candidatos com descritores e filtros; depois comparar a foto do cliente com imagens dos candidatos mais prováveis. Retornar diferenças e incertezas estruturadas. Não transformar aparência em confirmação de referência, cristal, diâmetro ou disponibilidade sem evidência do catálogo.

### 6. Ainda há texto de atendimento fixo no código

Há fallback comercial literal em apply_search_products_to_result e instrução comercial literal no responder. A migração anterior para banco não cobre integralmente todas as ramificações.

Proposta: inventariar as mensagens restantes e migrá-las para o catálogo editável. Manter no código invariantes de integridade e isolamento; permitir aos operadores alterar comportamento comercial, tom, perguntas e instruções dentro de parâmetros validados.

### 7. Testes de software não medem sozinhos a conversa

Os 2.307 testes da correção anterior verificam muitas regras, mas não comprovam uma taxa de qualidade generativa. A própria conversa reportada passou por um avaliador sem atender bem ao cliente.

Avaliar também escolha de ferramentas, argumentos, preservação de produto/variante, mudança de intenção e estado final. Um juiz LLM é apoio, não autoridade única. A [documentação de avaliação de agentes](https://developers.openai.com/api/docs/guides/agent-evals) recomenda analisar rastros completos e depois consolidar datasets repetíveis.

## Onde cada tecnologia ajuda

| Tecnologia | Uso recomendado no NSAgent |
|---|---|
| Responses API atual | Manter como base do atendimento e dos experimentos |
| Agents SDK | Piloto de uma capacidade isolada, para padronizar ferramentas, rastros e execução |
| Agents API | Auditoria de conversas e investigação de falhas fora do caminho de resposta ao cliente |
| OpenAI Developers plugin | Apoio ao desenvolvimento, documentação e diagnóstico da integração |
| Docs MCP | Acesso à documentação durante o desenvolvimento |

A Agents API gerencia sessão e ciclo de execução; pode operar sem sandbox usando environment.type=none. Para auditoria que precisa de scripts e artefatos, um ambiente de execução pode ser útil. [Arquitetura](https://developers.openai.com/api/docs/guides/agents-api/architecture).

Há uma consideração importante para a configuração avançada: agentes salvos são copiados para sessões, e mudanças posteriores não atualizam automaticamente sessões existentes. A página de configuração informa que instruções e ferramentas não podem ser alteradas pelo endpoint de atualização de sessão descrito. Precisaríamos de estratégia explícita de versão e renovação. [Configuração](https://developers.openai.com/api/docs/guides/agents-api/configuration).

O [quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart) utiliza beta.agents e o cabeçalho agents=v1. Isso reforça a conveniência de começar por um piloto isolado, sem migração ampla do atendimento.

O [plugin de desenvolvimento](https://developers.openai.com/learn/developers-codex-plugin) ajuda a construir aplicações. O [Docs MCP](https://developers.openai.com/learn/docs-mcp) oferece leitura da documentação e não executa a API do NSAgent nem melhora automaticamente suas respostas.

## Especialistas propostos

Manter Crono como responsável pela resposta final. Especialistas devolvem dados estruturados e são chamados somente quando necessários. Esse desenho corresponde a agents as tools na [documentação de orquestração](https://developers.openai.com/api/docs/guides/agents/orchestration).

- Catálogo: interpreta restrições, consulta candidatos, confirma características e informa lacunas. Parte significativa já existe como serviço determinístico; não precisa virar uma nova chamada LLM em todos os turnos.
- Visão: descreve a imagem e compara candidatos em casos ambíguos.
- Checkout: capacidade transacional com SKU, variante, CEP e carrinho validados. Preserva idempotência e separa compra atual de pedidos anteriores.
- Conhecimento institucional: recupera somente a política pertinente e sua versão.
- Auditor de qualidade: recebe histórico e rastros, identifica divergências, propõe casos de teste e ajustes. Não altera produção automaticamente a partir de uma opinião gerada.

Não executar todos em sequência a cada mensagem. Começar com um agente principal e ferramentas bem definidas; separar especialistas apenas quando os testes demonstrarem benefício. [Conceitos de construção de agentes](https://developers.openai.com/tracks/building-agents).

## Modelos a comparar

Preços de texto Standard por milhão de tokens, entrada sem cache/saída, consultados na documentação em 23/09/2026. Não incluem ferramentas, áudio, containers, impostos ou particularidades regionais/cache.

| Modelo | Entrada USD | Saída USD | Papel no experimento |
|---|---:|---:|---|
| [gpt-5.4-mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini) | 0,75 | 4,50 | Baseline atual; testar também esforço low |
| [gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | 0,10 | 0,50 | Candidato econômico para extração e tarefas delimitadas |
| [gpt-6-sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | 2,00 | 10,00 | Candidato principal para decisões complexas e resposta contextual |
| [gpt-6-astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | 10,00 | 50,00 | Referência de qualidade em amostra pequena e auditoria difícil |

Minha recomendação inicial é comparar gpt-5.4-mini otimizado com Luna e Sol. Não recomendo Astra para toda mensagem de WhatsApp. Não foi executado benchmark pago nesta análise; não há evidência ainda de que qualquer candidato supere o atual no catálogo da loja. Disponibilidade e limites da conta também precisam ser verificados antes do piloto.

O modelo atual suporta visão, ferramentas e saída estruturada; não é inadequado por definição. medium aplicado amplamente pode custar mais do que o necessário para redação e extração simples. A própria documentação de [otimização de latência](https://developers.openai.com/api/docs/guides/latency-optimization) destaca redução de chamadas e tokens, além da escolha do modelo.

## Plano incremental recomendado

### P0 — Contratos e referência de qualidade

- Consolidar resultado comercial aprovado/rejeitado e persistência de estado.
- Definir erros críticos: produto errado, variante trocada, preço/URL inventados, checkout incorreto e perda de informações já fornecidas.
- Ampliar critérios do avaliador para impedir aprovação por mera plausibilidade textual.
- Manter os casos históricos e criar variações com marcas e parâmetros diferentes.

### P1 — Custo e seleção de modelos

- Configurar por função modelo, esforço, limite de saída, timeout, orçamento e condições de escalonamento.
- Adaptar capacidades e validação para os modelos candidatos.
- Reduzir o contexto por etapa e medir cache/tokens.
- Revisor LLM apenas quando necessário; validações objetivas de identidade, preço, características, disponibilidade e URL permanecem obrigatórias.

### P2 — Comparação controlada

- Fixar versão do código, persona, políticas e respostas simuladas do catálogo.
- Começar com 20–30 conversas representativas: seleção curta, variação, imagem, orçamento, estoque, frete, compra, mudança de produto e falha de integração.
- Comparar inicialmente apenas duas configurações para controlar custo; ampliar candidatos após o primeiro resultado.
- Separar conjunto de ajuste e validação; repetir casos críticos.
- Avaliar resposta, ferramentas, argumentos, estado final, custo e latência.
- Configurar teto de gasto e parada antes de executar; não criar regressão automática recorrente.

### P3 — Piloto do SDK e auditor

- Encapsular uma capacidade de leitura, como confirmação de catálogo, atrás de interface compatível.
- Preservar canal, banco, persona e regras de checkout existentes.
- Medir a diferença frente ao fluxo atual; adotar somente com ganho demonstrado.
- Adicionar auditor independente, acionado manualmente ou por política posteriormente autorizada.

### Critérios de liberação propostos

- Mais de 90% de conversas aprovadas em validação separada, com tamanho e composição da amostra publicados.
- Zero erro crítico observado no conjunto de liberação; isso não equivale a garantia universal.
- Preservação de produto, variante e CEP nos casos de continuidade.
- Mudança explícita de produto libera a seleção anterior sem apagar dados válidos do cliente.
- Custo por conversa e p95 de latência dentro dos limites configurados.
- Revisão humana de amostra de respostas, além dos critérios automáticos.

## Arquivos relevantes

- app/llm/openai_gateway.py: capacidades, transporte e controles.
- app/llm/openai_models.py: resolução por função, atualmente main/fast.
- app/llm/prompt_compiler.py: composição de persona, conhecimento e memória.
- app/sales_agent.py: interpretação e roteamento.
- app/sales/responder.py: contexto e composição da resposta.
- app/verify/response_critique.py: revisão e eventual alteração de evidências/estado.
- app/verify/final_response.py: validação do texto entregue e evolução de estado.
- app/catalog/vision/product_image_index.py: descritor visual e embedding textual.
- app/evaluation/regression_runner.py: execução versionada de cenários.
- app/evaluation/regression_score.py: critérios de aprovação da campanha.

Esta análise combina fatos observados no repositório/banco com recomendações de arquitetura. Não houve mudança de modelo, teste com modelo real, instalação de plugin ou criação de automação.
