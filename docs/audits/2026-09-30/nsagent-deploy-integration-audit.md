# Auditoria dos deploys e integrações do NSAgent

Este documento registra a situação anterior às correções. Consulte o [relatório de correções de 01/10](nsagent-audit-corrections.md) para o resultado da execução e as pendências restantes.

O NSAgent recebeu os deploys e continua atendendo, mas o conjunto ainda não pode ser considerado totalmente validado. Há duas falhas reproduzíveis na suíte, publicação sem aprovação da CI, rotinas agendadas sem configuração e funcionalidades cuja eficácia em atendimento real ainda não foi demonstrada.

Esta auditoria cobre **30/09/2026, de 00h00 a 23h59, em Brasília**. Foram confirmados **19 deploys de produção, todos com status de publicação `success`**, pelos registros de deployments do GitHub emitidos pela Vercel. A verificação terminou após a virada do dia e também conferiu, como complemento, o deploy do link do grupo publicado às **00h00min30s de 01/10**. BI está excluído, conforme solicitado. Backend, frontend e TRAYadaptor entram somente como dependências do atendimento.

## Pendências confirmadas

**1. A publicação não está condicionada à validação automática.** Dos 19 commits publicados no período, 18 têm a execução da CI encerrada com falha; somente `0b30134` passou. A versão `c8ddcb5` foi publicada às 23h47min25s e seu teste de qualidade falhou às 23h47min28s. Isso comprova que publicação bem-sucedida não funciona como aprovação dos testes. O pacote, a configuração de deploy, a varredura de segredos e as avaliações offline passaram nessa execução. [CI de c8ddcb5](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36807439694)

Há duas falhas atuais, reproduzidas tanto antes como depois da atualização do link do grupo:

- `app/catalog/vision/image_product_id.py:717` importa `app.sales.after_sales_support`, contrariando o contrato arquitetural que impede catálogo de depender de vendas. Foi introduzido em `7efd10b`, às 17h12. É uma quebra real da regra do projeto; não foi observado erro de importação em produção.
- `tests/test_agent_flow_integration.py:180` espera o histórico antigo, sem o prefixo de identificação da IA. A implementação de `3ceca7a` passou a persistir esse prefixo junto da resposta. A divergência observada é de expectativa do teste; não demonstra perda de memória. A própria CI desse commit já acusou essa falha. [CI da identificação da IA](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36743465727)

Os dois deploys iniciais falharam em outro teste, de isolamento de Story, por um acesso a banco sem mock. Essa falha foi superada por `0b30134` e não reapareceu na suíte atual. Portanto, as 18 CIs vermelhas não significam 18 defeitos independentes.

**2. O aprendizado periódico está atrasado e os agendamentos do GitHub não chegam ao agente.** No período, houve cinco falhas de `Attendance Learning` e cinco de `Remarketing`. Os logs encerram antes da chamada HTTP com `Missing AGENT_BASE_URL or CRON_SECRET secrets`. A consulta aos nomes dos secrets do repositório confirmou a ausência de ambos. O cursor de aprendizado do tenant `newstore` registra a última execução em **15/09**, ainda apontando para resposta de **12/09**.

É uma pendência anterior aos deploys desta auditoria. O `vercel.json` também contém horários diários para essas rotinas; isso não comprova que a frequência pretendida de 15 minutos esteja sendo cumprida. No aprendizado, o cursor antigo confirma que o processamento incremental não está atualizado. Para remarketing, não havia tentativas registradas no recorte consultado para New Store, portanto não é possível quantificar contatos perdidos. [Falha de aprendizado](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36795356545), [falha de remarketing](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36801135468)

**3. A base de referências dos Destaques existe, mas está vazia.** A tabela `ai_story_highlight_references` e a migração estão em produção. O contrato de campos e rotas foi conferido entre o painel, backend e leitura pelo NSAgent. Não havia nenhuma referência cadastrada no momento da consulta. Assim, a implementação está disponível, mas ainda não acrescenta pistas ao reconhecimento. O cadastro é requisito operacional para obter o benefício dessa mudança.

**4. A eficácia do reconhecimento de Stories ainda não está comprovada na versão final.** Os quatro registros de processamento encontrados estavam `ready`, com vídeo arquivado, áudio transcrito e candidatos de catálogo. Todos tinham **zero identidades exatas aprovadas**. São registros de versões diferentes, incluindo reprocessamentos; não representam quatro casos independentes de sucesso.

O código atual usa `story-worker-v5`, enquanto os resultados persistidos eram das versões v1 a v4. Não apareceu novo atendimento de Story depois de `c8ddcb5` na amostra consultada. Portanto, os testes dão evidência da correção de seleção Mido, preservação de modelo e resposta alternativa útil, mas ainda falta um caso real posterior ao deploy para confirmar o resultado completo. O critério conservador de identidade impede afirmações sem prova; concluir a análise não equivale a identificar corretamente o relógio.

## Integrações e efeitos observados

| Implementação | Evidência verificada | Conclusão |
| --- | --- | --- |
| Publicação do NSAgent | 19 registros `Production` com sucesso; health HTTP 200 e SHA correspondente | Integrada no deploy |
| Configuração multimodal e domínios | Migrações presentes, worker ativado, limite de mídia de 100 MB, configuração 9 com os dois domínios da loja | Configuração integrada |
| Filas e retomada | 94 entradas processadas e 78 saídas enviadas no serviço; nenhuma pendência encontrada; agendamento de despacho ativo a cada 15 segundos | Sem fila presa no momento da consulta; os totais incluem os workspaces do serviço |
| Entrega da New Store | 42 respostas no Instagram em 30/09, todas com aceite do provedor | Envio observado; aceite não comprova leitura nem qualidade da resposta |
| Identificação da IA | 41 das 42 respostas com marcação aplicada; a restante antecede o deploy da identificação | Efeito observado |
| Fotos de produtos | Resposta 1045 tem comprovante de imagem aceito pelo Instagram; backend lê somente imagens com esse comprovante | Efeito de envio observado; renderização do painel não foi exercitada em navegador autenticado |
| Atendimento humano | Resposta 1063 tem `handoff.confirmed=true`, ação `mark_for_human` e mensagem aceita | Transferência observada |
| Vídeo e áudio de Story | Quatro resultados prontos, mídia privada e transcrição; resultado mais recente com nove candidatos | Infraestrutura observada, identidade ainda ambígua |
| Pronta entrega via adaptador | Consulta real completa no registro 1059; separação entre anúncio público e preço/estoque do catálogo; testes do adaptador passaram | Transporte funcionou; a consulta desse registro ainda era `mido`, anterior à correção final |
| Seleção por marca, região, cor e foto posterior | Testes de Stories, vendas e validação incluídos na suíte completa; regiões e verificações independentes conferidas no código | Validação automatizada, sem amostra real suficiente na versão final |
| Políticas, importação e desconto informativo | Testes de regressão passaram na suíte atual; política publicada é exigida para respostas determinísticas | Código validado por testes; não há amostra real suficiente para declarar todos os caminhos comprovados |
| Referências dos Destaques | Tabela e migração presentes; campos `name`, `url`, `active`, escopo de workspace e tenant compatíveis | Integração estrutural disponível, base vazia |
| Correção do falso “aguarde” | Testes de seleção Mido e de validação factual passaram; código preserva a falha original e pede informação útil | Corrigida no código; confirmação em conversa real posterior ainda pendente |

As verificações de dependências retornaram HTTP 200 no backend e no TRAYadaptor. O adaptador informou o SHA `8c3471cb008c`, esperado. Não foram encontrados logs de erro pelos filtros de severidade e exceção consultados no Render para esses dois serviços. A ausência nesses filtros não prova ausência absoluta de erros.

## Resultado dos testes

| Alvo | Resultado |
| --- | --- |
| NSAgent em `c8ddcb5` | 2.791 passaram, 2 falharam, 7 ignorados |
| NSAgent em `64615f2`, atualização complementar | 2.795 passaram, as mesmas 2 falhas, 7 ignorados |
| Avaliações offline em `c8ddcb5` | 102 passaram, 1 ignorado; job remoto aprovado |
| Chatbo backend | 273 passaram |
| TRAYadaptor | 264 passaram |
| Chatbo frontend | 22 passaram; build e verificação SEO concluídos |

A suíte completa do NSAgent foi executada com OpenAI e banco de produção desabilitados e sem parar no primeiro erro. Isso ampliou a cobertura em relação à CI, que interrompe na primeira falha. Os testes com dependências simuladas não substituem a validação de atendimento em produção.

## Inventário dos deploys de 30 de setembro

Todos os horários abaixo são de Brasília e correspondem ao registro de publicação em produção. A coluna CI informa a execução associada ao commit, não o status do deploy, que foi `success` em todos os casos.

| Horário | Commit | Mudança | CI |
| --- | --- | --- | --- |
| 00:00:39 | `7f79538` | Decodificação de vídeo de Story e identidade | Falhou |
| 00:28:57 | `b6ae0d3` | Consulta de pronta entrega pelo adaptador | Falhou |
| 03:14:10 | `0b30134` | Arquivo privado de mídia e fotos de contatos | Passou |
| 13:23:23 | `3ceca7a` | Identificação da IA e transferência humana | Falhou |
| 13:45:15 | `74f284b` | Perguntas de preço do Story e reconhecimento inconclusivo | Falhou |
| 16:12:36 | `59c38c0` | Envio de fotos de catálogo e singular | Falhou |
| 17:13:42 | `7efd10b` | Contexto de Story e suporte de importação | Falhou |
| 18:04:57 | `2754cd3` | Pronta entrega com contexto da conversa | Falhou |
| 19:42:23 | `e127968` | Evidências do vídeo e seleção do cliente | Falhou |
| 21:09:27 | `181a11b` | Política do produto e desconto informativo | Falhou |
| 21:09:59 | `d3f263f` | Publicação da configuração multimodal | Falhou |
| 21:59:20 | `7ae8291` | Worker durável e cache de evidências | Falhou |
| 22:00:12 | `3e6d3de` | Referências completas pronunciadas no vídeo | Falhou |
| 22:13:45 | `a1b7261` | Busca independente de cada relógio | Falhou |
| 22:14:54 | `d3beb2f` | Seleção pela marca mencionada | Falhou |
| 22:22:12 | `1a7c423` | Verificação independente por relógio e quadro | Falhou |
| 23:16:57 | `6ddb2a6` | Identidade preservada em fotos de continuação | Falhou |
| 23:37:09 | `2a8fbcd` | Referências dos Destaques na busca | Falhou |
| 23:47:25 | `c8ddcb5` | Busca Mido e continuidade da validação factual | Falhou |

## Atualização que entrou durante a auditoria

`64615f2`, resposta com o link oficial do grupo, foi commitado às 23h59min52s e publicado às **00h00min30s de 01/10**. Por horário de deploy, fica fora dos 19 de 30/09. Seu deployment retornou sucesso, a migração estava aplicada e a configuração passou para versão **10**, mantendo os domínios anteriores. O health passou a informar `64615f2fcb14`.

Os quatro testes novos passaram na suíte de 2.795 aprovações. Uma verificação local adicional passou os dois links pelo validador factual em modo obrigatório: duas afirmações verificadas, nenhuma violação. Não havia conversa posterior demonstrando esse caminho em produção. As duas falhas gerais da suíte continuam presentes.

## Ordem recomendada de correção

1. Corrigir a dependência catálogo → vendas e atualizar o teste do histórico para validar a identificação intencional da IA. Condicionar a publicação à CI aprovada.
2. Restaurar a configuração dos agendamentos e validar o avanço real do cursor de aprendizado; verificar o atraso acumulado antes de retomar envios de remarketing.
3. Preencher a base de referências e validar casos de Story com resultados conhecidos: vários relógios, seleção “o Mido”, pedido de preço, foto de continuação e nenhuma correspondência.
4. Exigir evidência de execução da versão v5 e de resposta final útil; acompanhar separadamente conclusão do processamento, identificação correta e entrega.

## Limites e evidências

O endpoint público ainda informa **um aviso de configuração**. O motivo exato exige o diagnóstico administrativo do servidor, que não foi acessado. Logs completos da execução Vercel também não estavam disponíveis pelo conector; a verificação usou health, deployments, CI e rastros persistidos no banco. Não houve teste de interface autenticada nem envio de mensagem de teste a clientes. Não foi executada compra ou alteração comercial.

Os dados e contagens foram coletados entre aproximadamente 23h49 de 30/09 e 00h04 de 01/10. A auditoria distingue defeitos atuais, pendências anteriores e ausência de evidência; não atribui indiscriminadamente todos os problemas aos deploys do dia.

Arquivos de apoio no mesmo diretório: `nsagent-deployments-ci.json` contém os 19 deployments, execuções da CI e a publicação complementar; `nsagent-audit-evidence.json` registra métricas e limitações sem mensagens de clientes; `nsagent-ci-causes.json` registra as causas das falhas iniciais. Os resultados completos das suítes ficam nos arquivos `.pytest-deploy-audit-20260930.txt` e `.pytest-deploy-audit-final-20260930.txt` na raiz do NSAgent.
