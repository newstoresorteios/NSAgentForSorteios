# Worker de análise de Stories

Cada workspace/conta/Story/versão possui um job em `instagram_story_analysis_jobs`.
O inbox recebe e persiste a mensagem antes de enfileirar a análise. Enquanto o
job está pendente, a conversa aguarda sem gerar uma resposta de falha, consumir
tentativas de entrega ou misturar mensagens de conversas diferentes.

O dispatcher existente, recuperado pelo pg_cron a cada 15 segundos, processa
inbox, outbox e a fila de Stories. `/api/cron/process-stories` permite drenar
um job separadamente com a mesma autenticação interna de `process-queues`.
Não depende de uma thread solta sobreviver ao fim de uma função serverless.

## Processamento

1. Download HTTPS validado, em arquivo temporário, com teto padrão de 100 MiB
   (104.857.600 bytes), conferido pelo cabeçalho e pelo conteúdo efetivo.
2. Reuso de cópia privada do workspace quando disponível. Falha no armazenamento
   não descarta a mensagem nem impede analisar um arquivo já baixado.
3. Frames distribuídos ao longo do vídeo, priorizando nitidez; transcrição do
   áudio; análise conjunta de frames, instantes, texto no relógio, arte e fala.
4. Consulta ao catálogo pelo TRAYadaptor. Uma segunda leitura compara os frames
   originais com as fotos dos candidatos, sem receber suas referências escritas.
   Cada relógio é consultado separadamente, com sua marca, textos e cor; o worker
   cobre até seis regiões por análise e revisa até doze candidatos distintos.
   O índice inclui produtos indisponíveis para identificação, sem revalidar preço
   ou estoque a partir desses registros. Consultas iguais compartilham a resposta.
5. Persistência das evidências e candidatos. Nenhum preço, estoque ou resposta
   personalizada entra no cache compartilhado. Esses dados são consultados ao vivo.

O vídeo completo não é enviado ao modelo: são usados frames representativos e
áudio limitado à duração configurada. Isso não garante observar cada detalhe
de cada frame. A precisão semântica exige avaliação com Stories reais rotulados.

## Identidade compartilhada

Uma associação automática exige referência/SKU/EAN lido independentemente nas
duas passagens, presente no catálogo e legível em pelo menos dois frames de
vídeo. Também aceita uma referência completa falada, encontrada literalmente na
transcrição, quando existe uma única peça visível e a revisão confirma a foto
oficial em dois frames com pelo menos três características distintivas.
Conflitos, variantes indistinguíveis e má qualidade impedem a aprovação.
Sem identificador legível ou referência falada corroborada, a semelhança visual permanece uma hipótese e o cliente
recebe uma pergunta de esclarecimento. Uma confirmação administrativa explícita
continua sendo a fonte preferencial. Duas leituras de IA ainda podem errar;
esta política reduz a propagação do erro, não promete precisão absoluta.

Em Stories com vários relógios, a escolha por posição/cor pertence à conversa.
Escolher “o azul” não muda a associação de quem perguntar sobre “o preto”.
Desvincular/reprocessar apaga a análise antiga e avança a geração dos jobs.
Um worker anterior não consegue publicar na nova geração, e suas chaves Redis
deixam de ser consultadas imediatamente. A chave também muda quando modelo,
versão da análise, quantidade de frames ou controles de áudio são alterados.

## Operação e configuração

- Aplicar a migração `story_analysis_worker` antes de publicar o código.
- Configurar `STORY_REDIS_URL` (ou `REDIS_URL`/`REDIS_TLS_URL`) pelo gerenciador
  de segredos da hospedagem. Em conexões externas, usar TLS (`rediss://`).
- Redis guarda JSON das evidências por até 24 horas; o banco é a fonte durável
  por sete dias e controla exclusão mútua, tentativas e gerações. Ausência,
  reinício ou indisponibilidade do Redis não perde o trabalho.
- Originais que couberem ficam no bucket privado `conversation-media`, não em
  Redis. No plano Free do Supabase, o limite por objeto é 50 MB. Vídeos maiores
  continuam sendo processados em arquivo temporário; frames compactos ficam no
  Storage e transcrição/análise no banco/cache. Não é necessário contratar Pro
  para analisar esses vídeos. O original maior não fica arquivado integralmente.
- Falhas têm até três tentativas, com lease de 240 segundos, execução limitada
  a 210 segundos e pausa de 15 segundos entre tentativas. Falha terminal só é
  reutilizada por dois minutos. Resultados de workers que perderam o lease são
  rejeitados. Um processo morto na última tentativa não deixa o inbox preso.
- `/api/admin/instagram/stories/health` informa configuração de worker, tamanho
  aceito e presença de Redis. Logs `story.worker_completed`/`story.worker_failed`
  informam job, frames, estado do áudio e revisão sem URLs assinadas ou tokens.

Documentação consultada: [Stories na API Meta](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/#story-video-specifications),
[limites do Supabase Storage](https://supabase.com/docs/guides/storage/uploads/file-limits),
[Redis assíncrono](https://redis.io/docs/latest/develop/clients/redis-py/async/),
[limitações de visão](https://developers.openai.com/api/docs/guides/images-vision#limitations).
O limite de publicação da API Meta não é garantia do tamanho de toda mídia
recebida de Stories publicados pelo aplicativo nativo.
