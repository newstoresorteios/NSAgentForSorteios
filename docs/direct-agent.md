# Agente direto

`NSAGENT_ENGINE=legacy|direct` seleciona o caminho na primeira linha executável de
`app.message_pipeline.process_incoming_message`. O padrão versionado é `legacy`.
O caminho `direct` chama `app.direct.pipeline.process_direct_message` e
`DirectOpenAIAgent.run_turn`. Não executa Door, SalesInterpretation, council,
critique, regras de seleção/compra nem apresentação semântica do legado.

## Configuração

```env
NSAGENT_ENGINE=direct
DIRECT_OPENAI_MODEL=
DIRECT_MAX_ROUNDS=4
DIRECT_TIMEOUT_SECONDS=65
DIRECT_VECTOR_STORES={}
```

Modelo vazio reutiliza `OPENAI_MAIN_MODEL`; a chave é a mesma `OPENAI_API_KEY`.
As configurações direct são operacionais, não podem ser alteradas por políticas
do workspace. `OPENAI_API_MODE` não seleciona este caminho: direct usa Responses.
O total de execução OpenAI é limitado por tempo e rodadas, sem fallback ao legado.

## Contexto e dados

Conversations API mantém o contexto na OpenAI. O identificador é registrado nos
metadados de resposta já existentes. A busca local exige workspace, provedor,
canal, conversa e identidade; somente respostas entregues são usadas. Antes de
reutilizar uma conversa remota, seu último item precisa ser o item da última
resposta entregue. Qualquer divergência reconstrói o contexto a partir do histórico
entregue. Após 30 turnos, uma conversa nova recebe até 30 pares entregues. Isso
limita o crescimento, mas não constitui memória infinita; mídias antigas não são
recarregadas durante reconstrução. Preferências duradouras existentes são lidas
do repositório de memória com workspace explícito. Não há autoaprendizado de prompt.

Este caminho armazena conversas na OpenAI, independentemente do `store=False` do
legado. O histórico operacional local continua sendo a autoridade de entrega.
Conversas remotas antigas não são apagadas automaticamente neste lançamento.

Conhecimento publicado: políticas institucionais e anexos processados da persona.
Sem vector store, a ferramenta `search_knowledge` consulta esses documentos.
Para usar File Search hospedado, publicar via
`POST /api/admin/direct/knowledge/{workspace_uuid}`, autenticado com ADMIN_API_TOKEN.
A publicação retorna `vector_store_id`, hash e contagem, sem ativá-lo automaticamente.
Adicionar o ID à configuração `DIRECT_VECTOR_STORES={"workspace_uuid":"vs_..."}`.
Publicar novo snapshot quando os documentos mudarem e substituir o ID; a fonte local
é consultável mesmo com File Search. Um vector store nunca é escolhido pelo cliente.

Ferramentas comerciais públicas: buscar produto, detalhes e estoque, todas pelo
TRAYadaptor com autenticação existente. Nenhuma mutação ou consulta privada de pedido
é exposta ao modelo. Compras usam links oficiais retornados; pedidos privados são
encaminhados para atendimento humano com consentimento. Confirmação é verificada
no backend e não inferida de um número de lista pelo agente.

Imagens usam download restrito e entram no mesmo modelo. Áudio usa transcrição
existente. Vídeo de Story pede uma foto nesta versão; não chama a cadeia antiga de
análise visual e não finge ter identificado o vídeo.

## Teste sem enviar mensagens a clientes

`POST /api/test/direct`, Bearer ADMIN_API_TOKEN:

```json
{"workspace_id":"UUID_DO_WORKSPACE", "text":"Quero um relógio para casamento até 3 mil"}
```

Resposta contém texto, métricas e `session`. Reenviar a session no próximo turno
preserva a conversa de teste. Ela é assinada, vinculada ao workspace e expira em uma
hora. Não cria inbox/outbox, não marca entrega e não altera conversas de clientes.
É possível testar direct enquanto o motor de produção continua legacy.

```powershell
python scripts/chat_direct.py --url https://SEU_NSAGENT --workspace UUID_DO_WORKSPACE --env-file .env
```

Sequência sugerida: solicitação de produto → preferência → "não quero o segundo"
→ pergunta de garantia → retorno ao primeiro produto → pedido de humano.
Comparar naturalidade, uso das fontes, continuidade, latência e custo.

## Ativação e rollback

Validar testes offline e preview real primeiro. Publicar o código com legacy,
testar `/api/test/direct`, configurar o conhecimento e só então publicar uma
implantação com `NSAGENT_ENGINE=direct`. `/api/health` informa `agent_engine`.
Rollback: restaurar `NSAGENT_ENGINE=legacy` e reimplantar. Respostas já aceitas na
outbox são reenviadas sem regeneração, mesmo após troca de flag. Mensagens ainda
sem resposta aceita usam a flag vigente quando o processamento começa.

## Contrato Tray verificado

Por autorização explícita do usuário, foram usados o código e o OpenAPI gerado
localmente do TRAYadaptor no lugar de skills/MCP Tray indisponíveis nesta sessão.
Confirmados GET `/internal/products`, `/internal/products/{product_id}` e
`/internal/products/{product_id}/stock`. Nenhuma rota do adaptador foi modificada.
