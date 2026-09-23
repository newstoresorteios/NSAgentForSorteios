# Qualificação guiada pelo catálogo

Implementação de 23/09/2026. Configuração New Store publicada na versão 6.

## Funcionamento

O fluxo adaptativo faz uma consulta preliminar nas buscas exploratórias elegíveis. Compara características documentadas dos candidatos e escolhe um assunto ainda não perguntado que diferencie os resultados. A OpenAI recebe apenas o assunto, as opções observadas e as instruções da persona; os candidatos completos não entram no prompt da pergunta.

A terceira pergunta é um ponto de revisão, não uma autorização para apresentar qualquer produto. Pode haver uma quarta pergunta útil. Não há repetição do assunto depois de uma resposta desconhecida. Um pedido explícito para mostrar resultados encerra essa etapa e encaminha à consulta completa.

Busca por modelo ou referência, imagem, variante, compra, frete e pós-venda seguem os caminhos existentes. Consultas por categoria já prontas e pedidos genéricos com orçamento também preservam o fluxo atual. Não se faz consulta da loja inteira antes de um pedido consultivo sem características.

## Configuração avançada

- `adaptiveDiscoveryEnabled`: ativa a evolução adaptativa. Desativar retorna à qualificação contextual anterior; para retornar ao comportamento anterior a ambas as melhorias, desative também `contextualDiscoveryEnabled`.
- `adaptiveDiscoveryRules`: JSON com `reviewAfterQuestions` (3), `maxSearches` (4 consultas preliminares), `candidateLimit` (20), `cacheTtlSeconds` (300), `facets`, `criteriaLabels` e `showResultsPattern`.
- `facets`: assuntos e campos do catálogo usados para identificar diferenças. A prioridade resolve empates; o número de valores distintos determina a escolha inicial.
- `message.adaptive_discovery_instruction`: linguagem e regras da pergunta gerada.
- Mensagens `adaptive_discovery_limit`, `adaptive_discovery_unavailable` e `adaptive_discovery_unconfirmed`: contingências editáveis.
- `contextualDiscoveryRules.questions`: conserva as instruções e textos de contingência por assunto. O antigo `maxQuestions` só se aplica quando o modo adaptativo está desligado.

Os limites e a estrutura do JSON são validados pelo banco ao publicar. A leitura também trata configurações inválidas sem interromper o atendimento. A interpretação de expressões regulares é validada no agente.

## Memória, filtros e custo

O marcador persistido em `provider_response._agent_metadata.discovery_question` armazena assunto, tema, preferências e uma amostra limitada dos candidatos. O histórico recupera esse marcador apenas para a conversa atual. A mudança de marca/tema ou uma resposta concluída inicia outra descoberta.

O cache só é reutilizado dentro da validade e quando os filtros não foram ampliados. Respostas curtas como `38` depois de uma pergunta com a opção `38 mm` são vinculadas ao assunto correto. Ausência de preferência não restaura uma restrição antiga daquele assunto.

A amostra preliminar nunca é usada diretamente como oferta. Nenhum resultado preliminar significa consultar pelo fluxo completo, não declarar falta de estoque. Antes de apresentar, permanecem a consulta completa, a recuperação limitada de detalhes e a validação existente de preço/disponibilidade. O fluxo adaptativo bloqueia a flexibilização silenciosa dos filtros após uma falha de correspondência.

O limite de quatro consultas é da etapa preliminar; a consulta completa mantém seus próprios limites existentes. Para candidatos com dados incompletos, continua valendo `contextualDiscoveryRules.detailLimit`. Não foram criadas rotinas automáticas de regressão ou chamadas recorrentes ao modelo.

## Observabilidade

O evento `catalog.adaptive_discovery` registra a escolha da pergunta, candidatos avaliados/compatíveis, uso de cache e ponto de revisão. `response_metadata.adaptive_discovery` registra a decisão associada à resposta. Os snapshots do catálogo ficam fora do prompt para evitar custo desnecessário.

## Validação

A suíte com o fluxo ligado encontrou nove falhas de compatibilidade. Foram corrigidos o conflito entre a qualificação fixa e a adaptativa, buscas preliminares desnecessárias em fluxos existentes e o tratamento de tamanho subjetivo como medida obrigatória.

Depois das correções: 844 testes de catálogo, memória e compra passaram com ambos os controles ligados. Os testes específicos cobrem pergunta por diferença real, quarta pergunta, resposta curta, não sei, pedido direto, cache vencido, ampliação de orçamento, mudança de marca/conversa, falhas e limite de consultas. A simulação integrada percorre consulta → pergunta natural simulada → resposta 38 → consulta final.

Foi verificada a configuração efetiva do banco, versão 6, com seis campos na configuração avançada. Um teste transacional confirmou a rejeição de edição inválida sem modificar os valores publicados. As verificações de segurança do banco não apontaram a nova função.

A migração `supabase/migrations/20260923144617_adaptive_catalog_discovery.sql` foi criada pela CLI e aplicada no banco. O código continua local até push/deploy. A avaliação de linguagem usa respostas OpenAI simuladas; não houve validação paga com modelo real nem envio a clientes.

Resultado final da suíte geral: 2299 testes passaram, seis foram ignorados e oito avisos de depreciação já existentes foram emitidos.
