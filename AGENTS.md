# Contrato comercial com a Tray

Estas instruções se aplicam a qualquer alteração neste repositório.

## Fronteira obrigatória

- O NSAgent nunca chama diretamente a API REST administrativa da Tray e nunca recebe credenciais OAuth da Tray.
- Toda consulta ou mutação comercial deve passar pelo contrato interno do `TRAYadaptor`, autenticado por `TRAY_ADAPTER_TOKEN`.
- O MCP `tray-docs` e as skills `tray-*` são ferramentas de desenvolvimento e documentação; não fazem parte do fluxo de produção do agente.
- O MCP Storefront público da Tray não substitui o `TRAYadaptor` para estoque autoritativo, clientes, pedidos, pagamentos ou webhooks.

## Alterações de integração

- Antes de mudar `app/tray`, `app/commerce` ou fluxos que dependem da Tray, use `tray-visao-geral` e a skill específica do recurso.
- Consulte obrigatoriamente `tray.search_docs` no MCP `tray-docs` para confirmar o comportamento upstream.
- Valide primeiro o contrato HTTP interno exposto pelo `TRAYadaptor`; não copie envelopes da API Tray para o NSAgent.
- Mantenha mutações fora do tool loop livre do modelo. Carrinho, pedido, cancelamento e atualização devem continuar em fluxos determinísticos, com confirmação e política explícita.
- Não invente produto, preço, estoque, disponibilidade, pedido, frete ou link de pagamento quando o adaptador estiver indisponível.

## Testes de contrato

- Para cada rota adicionada ou alterada, cubra método, caminho, query/body, autenticação, timeout, erro normalizado e campos retornados.
- Confirme que o caminho existe no OpenAPI atual do `TRAYadaptor` antes de concluir a mudança.
- Preserve a distinção intencional entre operações registradas internamente e schemas expostos diretamente ao modelo.
