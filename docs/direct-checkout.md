# Checkout controlado no agente direto

O agente continua usando Responses API e suas ferramentas de consulta. A nova
ferramenta `prepare_checkout` apenas prepara uma revisão. A criação do carrinho
ocorre em `handle_checkout_command`, antes de chamar o modelo no turno seguinte.
Nenhuma ferramenta de criação, pagamento, alteração ou cancelamento de pedido
é exposta ao modelo.

## Fluxo disponível

1. O cliente escolhe um produto do catálogo administrativo já consultado.
2. O backend consulta produto/variação sem cache, exige disponibilidade, estoque
   suficiente e preço positivo. Produto com variação exige a variação escolhida.
3. Uma resposta determinística mostra produto, variação, quantidade, preço,
   subtotal e `CONFIRMAR <código>`. A proposta vale por dez minutos.
4. Somente a proposta registrada como entregue ao cliente pode ser confirmada.
   Workspace, provedor, canal, conversa e identidade devem coincidir.
5. O cliente envia o comando completo em texto, sem anexos. Um “sim” apenas pede
   o código; número de lista, negação, áudio e instruções em mídia não executam.
6. O backend grava uma tentativa exclusiva, revalida os fatos e chama o adaptador
   uma vez para criar o carrinho. Confere os itens e o preço no carrinho completo
   antes de apresentar o link HTTPS oficial retornado pelo adaptador.
7. Frete, pedido e pagamento são finalizados no site. O bot não afirma reserva de
   estoque, pedido criado ou pagamento confirmado com base no carrinho.

`CANCELAR <código>` ou uma recusa simples descarta a proposta ainda pendente.
Isso não cancela pedidos nem remove carrinhos já criados. Alterar o item, a
quantidade ou o preço exige uma nova proposta e confirmação. Este fluxo trabalha
com um produto/variação por carrinho, quantidade de 1 a 10.

Pronta entrega pública usa o link oficial do produto. Seus IDs não são aceitos
como produtos administrativos para checkout. Consultas de pedidos já existentes
continuam nas ferramentas de consulta do direto; mudanças e cancelamento de
pedidos permanecem com atendimento humano.

## Idempotência e entrega

`ai_direct_checkout_executions` usa o identificador da proposta como chave
primária e sessão do carrinho. O registro `started` é confirmado no banco antes
da mutação, em transação curta. A aplicação nunca repete o POST de uma proposta
já registrada. O adaptador mantém a reconciliação upstream que já implementava.

- `completed`: devolve o mesmo link registrado, sem recriar carrinho.
- `rejected`: fatos indisponíveis ou diferentes; exige nova revisão.
- `started`/`unknown`: pode ter havido execução; exige reconciliação humana.

Não existe garantia de transação distribuída entre Postgres e Tray. Uma queda
entre gravar a tentativa e concluir a operação pode exigir intervenção mesmo
quando nenhum carrinho foi criado. Essa escolha evita repetição cega. Não apagar
registros pendentes para tentar novamente: verificar primeiro a sessão no
adaptador. A rotina automática não reenvia mutações nem cancela recursos.

A outbox preserva a proposta e o workspace nos reenvios. Produtos conhecidos e
continuidade também sobrevivem; prompts e o identificador do último item remoto
não são copiados. Após resposta determinística ou reenviada, a conversa OpenAI
é reconstruída pelo histórico entregue.

## Ativação

O recurso vem desligado: `DIRECT_CHECKOUT_ENABLED=false`. A flag é operacional,
não pode ser alterada por persona, cliente ou políticas publicadas do workspace.

1. Aplicar `sql/037_direct_checkout_executions.sql` no banco dedicado do agente
   usando o proprietário de backend que executará as consultas. Se houver um
   papel de execução separado, revisar seus privilégios e política de acesso;
   a migração não cria acesso público. RLS está habilitada e `anon`,
   `authenticated` e `PUBLIC` não recebem acesso à tabela.
2. Publicar o código e habilitar `DIRECT_CHECKOUT_ENABLED=true` com o motor direto.
3. Validar preparação e confirmação no preview autenticado. O preview reconhece
   a confirmação sem gravar a tentativa e sem criar recursos comerciais.
4. Validar o fluxo real em ambiente controlado antes de ampliar o uso.

Para interromper novas criações, desligar a flag. Não excluir o registro de
execuções: ele é necessário para evitar repetição e investigar resultados incertos.
Nenhuma migração, flag de produção ou implantação foi executada nesta implementação.

## Verificação reproduzível

```powershell
python -m pytest tests/direct tests/ingress tests/commerce/test_cart_order_contract.py tests/commerce/test_cart_session_lifecycle.py tests/commerce/test_workspace_checkout_policy.py -q
node scripts/verify_direct_checkout_journal.mjs
```

O script SQL usa a dependência de desenvolvimento PGlite já usada por
`verify_memory_security_migrations.mjs`. Executa a migração duas vezes e as
consultas reais do repositório em PostgreSQL local, sem conexão de produção.
Verifica exclusividade da tentativa, escopo, recibos finais e bloqueio de CRUD
para os papéis públicos.

Os testes Python simulam OpenAI e HTTP do adaptador. Cobrem a conversa até a
revisão, execução fora do modelo, reenvio pela outbox, concorrência, interrupção,
falha do banco, preço/estoque alterados, isolamento, preview, URL e conteúdo do
carrinho, autenticação, método, caminho, corpo, timeout e erros normalizados.
Eles não demonstram qualidade linguística de um modelo real nem entrega real no WhatsApp.

Com autorização do usuário, foi usado o código/OpenAPI local do `TRAYadaptor`
no lugar das skills/MCP Tray indisponíveis. Verificados no OpenAPI gerado:

| Método | Caminho interno |
| --- | --- |
| GET | `/internal/products/{product_id}` |
| GET | `/internal/products/variants/{variant_id}` |
| POST | `/internal/carts` |
| GET | `/internal/carts/{session_id}/complete` |

Nenhuma rota do adaptador foi alterada. O acesso usa o `TRAY_ADAPTER_TOKEN`
existente, sem credenciais OAuth Tray no NSAgent.
