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
DIRECT_MEMORY_ENABLED=true
```

Modelo vazio reutiliza `OPENAI_MAIN_MODEL`; a chave é a mesma `OPENAI_API_KEY`.
As configurações direct são operacionais, não podem ser alteradas por políticas
do workspace. `OPENAI_API_MODE` não seleciona este caminho: direct usa Responses.
O total de execução OpenAI é limitado por tempo e rodadas, sem fallback ao legado.

## Persona e preferências

O agente recebe as instruções completas da persona ativa e todas as seções de
conteúdo do perfil ChatBo, sem truncamento silencioso. Isso inclui exemplos,
recomendação, objeções, identidade e orientações comerciais. Não executa gates,
classificadores ou reescritores do legado. Limites de ferramentas e segurança
prevalecem sobre instruções incompatíveis; o pedido atual prevalece sobre
preferências antigas. Memória orienta recomendações, sem impor marcas ou teto
de preço a uma busca ampla. Metadados registram hash da persona e número de
documentos disponíveis, sem registrar seu conteúdo nos logs.

Pronta entrega usa `search_ready_delivery`, via GET `/internal/ready-delivery`
do TRAYadaptor, cuja fonte é `www.newstorerj.com/pronta-entrega`. Uma tentativa
de usar `search_products(ready_stock=true)` retorna orientação para usar a
ferramenta correta, sem consultar o catálogo errado. Os resultados preservam
`evidenceType=public_listing` e `stockConfirmed=false`: não confirmam estoque
físico, preços ou prazo. Falhas de consulta não significam catálogo vazio.

A ferramenta retorna `total`, `returned`, `has_more`, `next_offset` e `snapshot_id`.
A IA escolhe a apresentação; a camada de transporte não corta mais as listas em
cinco itens. A continuação usa a mesma query e snapshot, com offset informado pela
ferramenta. O adaptador mantém até 32 snapshots públicos durante dez minutos;
expiração/reinício retorna 409 e exige atualizar a busca, nunca declarar falta de
produtos. O site pode mudar entre novas buscas; o snapshot estabiliza a paginação.

Produtos consultados (até 60) e a última página ficam nos metadados entregues.
`prepare_product_image` aceita somente URL de produto já consultado, com imagem
HTTPS no CDN da Tray. Somente a ferramenta seleciona as fotos: mencionar "foto"
na mensagem não anexa automaticamente produtos anteriores. Instagram usa o envio
nativo existente; WhatsApp usa upload binário e envio por media ID, descritos em
[whatsapp-native-media.md](whatsapp-native-media.md). O preview retorna a referência
sem enviar nada. Não recupera imagens por URL arbitrária e não substitui falha de
anexo por um link ao cliente.

## Contexto e dados

Conversations API mantém o contexto na OpenAI. O identificador é registrado nos
metadados de resposta já existentes. A busca local exige workspace, provedor,
canal, conversa e identidade; somente respostas entregues são usadas. Na
Brevo/WhatsApp, também se permite continuidade entre IDs
de conversa das últimas 24 horas quando workspace, provedor, canal, sender_key e
visitor_id são os mesmos. A sessão OpenAI é reconstruída ao mudar o ID da conversa,
preservando mensagens entregues e referências dos produtos. Outros canais não
recebem essa exceção. Antes de
reutilizar uma conversa remota, seu último item precisa ser o item da última
resposta entregue, e o hash do texto original deve coincidir com o texto efetivamente
entregue. Reescritas de handoff, links acrescentados e alterações do transporte
invalidam a reutilização. Metadados antigos sem hash também causam reconstrução.
Qualquer divergência reconstrói o contexto a partir do histórico
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
TRAYadaptor com autenticação existente. `lookup_order` reutiliza a consulta de
pedido do legado (`find_order_by_customer_document` e `get_order_facts`) em modo
somente leitura: GET `/internal/customers`, `/internal/orders` e
`/internal/orders/{id}/complete`. O modelo só pode passar número ou CPF/CNPJ que
o cliente escreveu, ou um número já confirmado nesta conversa. O telefone do
atendimento, o documento ou o vínculo do pedido confirmam o titular antes de
devolver status, previsão e rastreio. Não há criação, cancelamento ou alteração.
Compras usam links oficiais. Encaminhamento humano só entra na fila com
`request_human` bem-sucedido ou com pedido/aceite explícito do cliente; uma
promessa de transferência sem esse consentimento vira oferta e espera confirmação.

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

`scripts/eval_direct_conversation.py` executa uma bateria real e isolada: primeira
página, continuação no mesmo snapshot, ocasião, foto, reconstrução da conversa,
base de conhecimento e recusa de compra/handoff. Requer token administrativo do
preview e CLI Vercel autenticado; grava somente evidências de teste, sem tokens
ou sessões. Não cria inbox/outbox nem envia mensagens a clientes.

## Ativação e rollback

### Vídeo e comparação completa (direct-v5)

O caminho direto reutiliza download seguro, amostragem temporal de quadros e
transcrição de áudio do legado. `app/direct/media.py` entrega imagens com tempos
e transcrição ao mesmo agente; não executa o analisador visual ou a composição
do legado. Stories expirados tentam renovar a URL pela integração Meta existente.
Miniatura é explicitamente evidência parcial; falha não autoriza identificação.
Os limites existentes de bytes, duração e número de quadros continuam aplicáveis.
`direct.video.prepared` registra somente quantidade de quadros e estado do áudio.

Para recomendar, `compare_ready_delivery_catalog` percorre o snapshot inteiro via
GET `/internal/ready-delivery` em páginas de 50 e entrega nomes/referências de
todos os candidatos. Até 500 itens, com prazo de 35 segundos; snapshot divergente,
total inconsistente ou página faltante falham explicitamente. Não é um ranking
prévio nem uma seleção dos dez primeiros. A IA compara os candidatos e usa
`get_ready_delivery_candidate` para obter links e imagens de até dez escolhas.
Atributos ausentes, preços e estoque físico não são inferidos pelos nomes.
A paginação de dez permanece disponível para quem quer percorrer a lista.

Fotos aceitam `candidate_id` validado contra produtos conhecidos, evitando a
reescrita de URLs longas. Link exato continua aceito para conversas anteriores.

O preview autenticado aceita `channel` (whatsapp/instagram) e `video_base64`
opcional (MP4 de até 2 MB), processado em arquivo temporário e removido ao final.
Não envia mídia aos canais. `scripts/eval_direct_video_catalog.py` verifica a
comparação completa, foto em continuação e vídeo com fixture local.

Entrada multimodal segue o formato da [documentação oficial de visão da OpenAI](https://developers.openai.com/api/docs/guides/images-vision).

### Procedimento de ativação

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
