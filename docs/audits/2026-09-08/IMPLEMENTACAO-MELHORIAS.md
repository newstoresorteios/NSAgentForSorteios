# Implementação das melhorias da auditoria — 2026-09-08

Este registro relaciona as correções implementadas após a auditoria integrada do agente e do Tray Adaptor.

## Correções aplicadas no agente

- Memória de `recipient` passou a ser aceita somente no escopo da conversa. Registros legados desse tipo deixaram de ser injetados em novas conversas, eliminando o reaproveitamento indevido de nomes ou destinatários.
- O cliente do Tray propaga o `trace_id` do turno em `X-Request-ID` e registra os identificadores devolvidos pelo Render/Tray em `_agent_runtime`.
- O relatório de integridade passou a medir a presença de `_agent_metadata`, `_agent_runtime` e compilações de prompt nas respostas recentes.
- `/health` passou a expor o SHA curto do deploy e a contagem de advertências de configuração. O diagnóstico administrativo inclui as advertências sem revelar valores ou segredos.
- Foi criado um validador executável que garante que todas as variáveis aceitas por `Settings` estejam documentadas em `.env.example`.
- O workflow de aprendizado passou a mostrar status HTTP e um resumo seguro da execução, com timeout e falha explícita para respostas inválidas.
- O CI agora mede cobertura de linhas e branches, guarda `coverage.xml` e exige um piso inicial de 70%.
- A documentação explica a diferença entre o pool interno de candidatos e as três sugestões apresentadas ao cliente, além do procedimento para verificar versão e rastrear uma requisição.

## Correções aplicadas no Tray Adaptor

- O middleware aceita ou gera um identificador de rastreamento, devolve `X-Trace-ID` e registra método, rota mascarada, status e latência.
- Identificadores dinâmicos de carrinho, pedido, produto, categoria, marca e cliente são mascarados nos logs de rota.
- `/health` passou a informar o SHA curto do commit implantado.
- Dependências de teste foram removidas do pacote de produção e movidas para `requirements-dev.txt`.
- Foi criado um workflow de CI com compilação e suíte integral de testes.

## Validação

- Agente: 1.887 testes aprovados, 1 ignorado; cobertura total de 72,03% na suíte integral.
- Caminho equivalente ao CI do agente: 1.763 testes aprovados, 1 ignorado; piso de 70% atendido.
- Tray Adaptor: 232 testes aprovados.
- Regressões focadas de memória, rastreamento, configuração, KPI, diversidade de marcas e recomposição: 89 testes aprovados.
- Compilação Python e `git diff --check`: aprovados nos dois repositórios.

## Validação necessária após o deploy

O código não pode comprovar o comportamento de produção antes de receber tráfego novo. Depois da publicação, validar:

1. SHA exibido nos dois endpoints de saúde.
2. Uma conversa nova pedindo opções de marcas na mesma faixa de preço.
3. Ausência de nome ou destinatário herdado de outra conversa.
4. Presença do mesmo rastreamento nos registros do turno, Vercel e Render.
5. Execução bem-sucedida do workflow de aprendizado e avanço do cursor.
