# Incidente: consulta por automático com safira até R$ 2.500

## Conclusão

Em 15/09/2026, às 18h28–18h29 de Brasília, o NSAgent executou a consulta, recuperou produtos e depois substituiu a resposta por encaminhamento humano. A causa imediata foi uma falha na coordenação do limite interno de chamadas de IA com a revisão obrigatória. A revisão não chegou a avaliar o conteúdo: seu bloqueio operacional foi convertido em reprovação.

Há falhas adicionais confirmadas: perda dos requisitos técnicos na busca, produtos incompatíveis na seleção, estado de conversa incoerente com a mensagem enviada, encaminhamento que atingiu outro workspace e texto de atendimento ainda fixo no código. Aumentar o limite de chamadas isoladamente não resolve o incidente completo.

Esta investigação consultou logs Vercel/Render, banco em transações somente leitura, código do commit publicado e páginas públicas dos produtos. Foram feitas duas reproduções locais sem chamadas à IA ou envio de mensagens. Nenhuma correção ou alteração de produção foi aplicada nesta análise.

## Identificação e cronologia

| Campo | Evidência |
|---|---|
| Contato | Final 8149 |
| Mensagem | `ai_inbound_messages.id=806` |
| Resposta | `ai_agent_responses.id=787` |
| Trace | `e2d7851da8a64c1498e1a3a556691208` |
| Deploy Vercel | `dpl_7rqb3nbxPywvfcrvQpSv8vDU64Pj` |
| Commit | `d090df4459af21a70e2505544ba2031848cc27bc` |
| Recebimento | 18:28:30.832 BRT / 21:28:30.832 UTC |
| Resposta persistida | 18:29:10.564 BRT / 21:29:10.564 UTC |
| Duração HTTP | 39.932 ms, status 200 |
| Entrega | `provider_send_ok=true`; Brevo retornou 201 |

Pedido: “tem relogio automatico e com cristal de safira até 2500 reais?”

Resposta enviada: “Prefiro confirmar esses dados com a equipe antes de te responder com segurança. Um atendente humano pode te ajudar agora.”

O deployment já continha as correções anteriores. A persona versão 17 foi carregada do banco do workspace correto, com configuração versão 1 e 464 definições. O modelo efetivamente usado foi `gpt-5.4-mini`.

### O que as integrações fizeram

- Render/Tray: 15 chamadas correlacionadas ao trace, todas com HTTP 200. Incluem categorias, árvore, duas buscas, detalhes e variantes.
- Busca local: cinco candidatos, filtro registrado apenas por limite de preço, quantidade máxima de 30.
- Busca Tray por categoria: 16 candidatos. Busca por nome “relógio”: zero. Conjunto combinado: 18 candidatos; filtro/ranking manteve os 18.
- IA: três chamadas bem-sucedidas, de interpretação, seleção e regeneração. Nenhuma falha de transporte ou de integração registrada.
- Backend Render: as duas páginas de logs consultadas na janela 21:26–21:30:30 UTC mostram consultas da Central com HTTP 200. A resposta inadequada foi produzida pelo NSAgent antes da exibição.

Os 15 acessos à Tray somaram aproximadamente 15,93 segundos de duração individual; há sobreposição, portanto esse valor não deve ser somado diretamente às etapas para calcular o tempo total. A interpretação consumiu 8,30 segundos, a seleção 10,58 e a regeneração 3,61.

## 1. Causa imediata: revisor bloqueado pelo limite de chamadas

Sequência confirmada pelos logs e reproduzida localmente com `LLMCallBudget`:

| Operação | Resultado | Chamadas consumidas |
|---|---|---:|
| Interpretação | Permitida | 1/3 |
| Seleção dos produtos | Permitida | 2/3 |
| Primeira revisão | Bloqueada: última chamada reservada à composição | 2/3 |
| Regeneração da resposta | Permitida, consome a reserva | 3/3 |
| Segunda revisão | Bloqueada: limite esgotado | 3/3 |
| Encaminhamento | Resposta substituída | 3/3 |

O erro foi `critique_failed:LLMCallBudgetExceeded`, seguido de `response_critique_failed`. Isso identifica o limite do próprio agente, sem evidência de falta de créditos ou limite da API OpenAI.

Detalhes que explicam a incompatibilidade entre controles:

1. O banco configura `agent_critique_mode=shadow`, mas também `agent_critique_enforce_on_commerce=true`. A presença de preço promoveu a revisão para `enforce`.
2. O banco configura limite comum de três chamadas e complexo de quatro. `message_pipeline.py:235` reaplica o limite comum; a classificação posterior como `complex` não promove automaticamente o orçamento nesta execução.
3. `run_critique_judge` converte a exceção operacional em nota zero e `pass_check=false`. O laço trata isso como resposta ruim e tenta regenerar, sem que um revisor tenha identificado erro de conteúdo.
4. O banco permite quatro tentativas de crítica nesta configuração, mas `response_critique.py:969` limita o valor efetivo a uma regeneração por regra fixa. O painel e o comportamento efetivo divergem.

Referências: `app/ops/turn_runtime.py:38`, `app/llm/llm_call_policy.py:115`, `app/message_pipeline.py:233`, `app/verify/response_critique.py:462` e `app/verify/response_critique.py:1134`.

## 2. A consulta não preservou todos os requisitos

A interpretação persistida conservou R$ 2.500 e a expressão “cristal de safira” como atributo livre. “Automático” ficou no tópico descritivo e não nos campos consumidos pelos filtros. O índice foi consultado com `max_price=2500`, sem mecanismo. As amostras do ranking só registraram `budget_max` como requisito atendido.

O código explica por que isso pode acontecer mesmo quando a IA extrai corretamente o mecanismo:

- `turn_understanding_to_sales` recebe `hard_constraints.mechanism`, mas não o transfere às preferências operacionais.
- `_hard_constraints_from_interpretation` também não transporta esse campo ao filtro.
- `required_feature_groups` exige atributos com prefixos específicos; “cristal de safira” não gera uma exigência.
- A normalização que inclui `required_feature:automatico` e `required_feature:safira` está restrita à frase com alternativas “open heart ou skeleton”. Não abrange o pedido deste incidente.
- `product_compatible_with_requested_movement` permite um produto de quartzo mesmo quando recebe explicitamente o atributo “automatico”.

Reprodução local sintética: informei mecanismo automático, safira e orçamento de R$ 2.500; um produto explicitamente descrito como quartzo com cristal mineral, por R$ 2.200, passou pelo filtro. Isso confirma a falha do código; não reconstrói o conteúdo bruto original da saída do modelo, que não foi recuperado nesta análise.

### Produtos presentes na seleção final registrada

| Produto | Valor registrado no turno | Verificação de características |
|---|---:|---|
| Seiko SBTR019, ID 13434 | R$ 2.499,99 | A descrição da loja informa quartzo e Hardlex; incompatível com automático e safira. |
| Citizen BN0151-09L, ID 893 | R$ 2.499,99 | A descrição da loja informa Eco-Drive solar e cristal mineral; incompatível com o pedido. |
| Orient RA-AA0010B19B, ID 4917 | R$ 2.399,99 promocional | O índice não forneceu mecanismo/material. A página não pôde ser lida nesta verificação; não concluo compatibilidade. |

Fontes das características: [Seiko na loja](https://www.newstorerj.com.br/relogios-seiko/relogio-seiko-spirit-chrono-azul-sbtr019) e [Citizen na loja](https://www.newstorerj.com.br/relogios/relogios-citizen/relogio-citizen-promaster-eco-drive-professional-diver-azul-bn0151-09l). Os valores acima vêm do registro do turno. As páginas foram usadas para conferir especificações, sem inferir estoque atual a partir de elementos do HTML.

As três linhas do índice tinham `mechanism=null` e `material=null`. Não há comprovação de que a seleção tenha atendido todos os requisitos, nem de que todo o catálogo careça de uma opção adequada. Portanto, “não temos esse produto” também não seria conclusão sustentada por esta execução.

## 3. Encaminhamento atingiu 20 conversas em dois workspaces

O evento `handoff.queue.marked` retornou 20 IDs. A consulta desses IDs confirmou 19 registros no workspace do incidente e um em outro workspace, todos com `status=waiting` e `bot_activated=false` no momento da verificação.

`app/ops/handoff_queue.py:93` combina telefone e identificadores com OR, sem restringir workspace, canal ou sessão. O log comprova que essas linhas foram atualizadas pelo encaminhamento. O banco atual não permite afirmar que todas tinham bot ativo antes da operação.

Impacto: o efeito da falha ultrapassou a sessão que originou o pedido. Deve-se restringir a atualização à conversa canônica do workspace/canal e reconciliar os registros afetados usando o histórico de atendimento. Reativar todos indiscriminadamente poderia interferir em atendimento humano legítimo.

## 4. Memória contradiz a mensagem enviada

Apesar do texto de encaminhamento, os metadados finais conservaram:

```json
{
  "presented_products": true,
  "product_resolution_state": "options_presented",
  "dialogue_phase": "shortlist"
}
```

O conselho aprovou o rascunho antes da substituição; o DoubleCheck foi pulado por `human_handoff`. A validação factual registrada verificou três valores monetários, sem provar automático/safira. Essa combinação permite interpretar uma próxima mensagem como escolha de opções que o cliente não recebeu.

## 5. Persona e controles dos operadores

A persona foi carregada corretamente, mas a resposta final veio de uma string fixa em `app/verify/response_critique.py:1149`, repetida em `:1174`. Alterar somente a persona não muda esse texto. O catálogo consultado não contém uma definição dessa mensagem específica.

Também há divergência entre configuração publicada e execução: modo shadow promovido para enforce, limite complexo não aplicado e tentativas limitadas por constante. O painel deve mostrar tanto a configuração quanto o valor efetivo e sua justificativa, com validação de combinações incompatíveis antes da publicação.

## Ordem recomendada de correção

1. **P0 — isolar o encaminhamento:** workspace, canal e sessão canônica; revisar os 20 registros sem reativação indiscriminada.
2. **P1 — preservar os requisitos técnicos:** mecanismo, cristal, orçamento e evidência por produto do entendimento até a resposta. Separar correspondência confirmada, incompatibilidade e informação desconhecida.
3. **P1 — coordenar orçamento e revisão:** reservar capacidade antes da seleção opcional; aplicar promoção de limite quando cabível; diferenciar revisor indisponível de reprovação de conteúdo; não regenerar por falta de orçamento do próprio revisor.
4. **P1 — validar texto e estado finais:** o texto realmente enviado deve atender ao pedido e atualizar memória, produtos apresentados e encaminhamento de forma coerente.
5. **P1 — concluir controle pelo banco:** mensagem de contingência, regras de revisão e limites efetivos editáveis/versionados, respeitando a persona publicada e com histórico de alteração.
6. **P2 — reduzir latência e medir qualidade:** evitar regeneração inútil, consultar detalhes dos candidatos relevantes e criar avaliação com conversas reais, incluindo este caso e a mensagem seguinte.

Critérios mínimos de regressão: quartzo/mineral excluídos quando automático/safira forem obrigatórios; características desconhecidas não apresentadas como confirmadas; limite interno não classificado como defeito de conteúdo; encaminhamento restrito ao workspace/sessão; metadados compatíveis com o texto entregue; mensagem alterável no banco; fluxo completo testado também com modelo real, além dos testes locais.

## Evidências locais

- `contato-8149-safira-evidencias.json`: interpretação, revisões, metadados e métricas persistidas da resposta 787.
- `safira-analise-complementar.json`: catálogo e situação atual dos IDs atingidos pelo encaminhamento.
- `safira-budget-reproducao.json`: reprodução da sequência de orçamento.
- `safira-filtros-reproducao.json`: reprodução sintética da perda de mecanismo e passagem de quartzo/mineral.

Esses arquivos de auditoria ficam locais e não foram incluídos em commit/push.
