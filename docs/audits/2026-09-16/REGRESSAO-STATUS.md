# Regressão conversacional — checkpoint de 16/09/2026

## Estado atual: candidato 11, aguardando recuperação da cota da API

**A campanha continua incompleta. Não há comprovação de maturidade superior a 90%.** O provedor voltou a retornar `quota_exhausted` no candidato 10. Uma chamada isolada posterior, sem concorrência, repetiu o bloqueio. As execuções foram interrompidas; não houve tentativa de completar os casos com respostas artificiais para contabilizar aprovação.

O comportamento escolhido pelo usuário foi exercitado com o avaliador real antes da interrupção:

- Reclamação → oferta de transferência → recusa: aprovado nos dois turnos. O bot continua e explica seus limites, sem executar transferência nem perguntar genericamente o nome.
- Avaliação de relógio usado → oferta → aceite: aprovado nos dois turnos. A transferência só é autorizada após o aceite.

Isso valida os dois fluxos no candidato 10, **não** representa aprovação de toda a campanha nem a execução de uma transferência real no WhatsApp.

### Validação concluída

**2.182 testes locais passaram; 6 foram ignorados.** Oito avisos são de depreciação do adaptador SQLite usado pelos testes. Uma execução local intermediária falhou por falta de espaço no volume dos temporários; foi repetida com temporários no diretório privado do projeto, sem apagar arquivos do usuário.

Os 107 casos completos do candidato 8 permanecem como último lote amplo: 66/72 no desenvolvimento, 10/18 no contato 8149 e 9/17 no contato 0859. Não foram combinados os melhores resultados de candidatos diferentes para produzir uma nota.

Na repetição dirigida do candidato 9, passaram 4/6 casos controlados, 2/8 do contato 8149 (mais 5 falhas e 1 inconclusivo) e 3/8 do contato 0859. Houve **uma tentativa de criar carrinho ao refinar a cor do Baltic**, bloqueada pela simulação. O candidato 10 acrescentou uma proteção testada localmente para pesquisar a variante correta antes de selecionar compra; a confirmação com IA real ainda está pendente.

O candidato 10 parou com 2 casos aprovados, 5 inconclusivos por cota e 15 pendentes no lote dirigido. O candidato 11 acrescenta o reconhecimento explícito de perguntas de disponibilidade e preserva a resposta de venda desabilitada mesmo quando a ficha mostra estoque positivo e uma observação de prazo. **O candidato 11 não possui rodada generativa concluída.**

### Alterações finais preparadas

- Reconhecimento do produto recuperado no histórico é ligado à referência usada pela próxima consulta de mídia.
- Uma resposta de nome preserva o assunto e não vira seleção de compra; a revisão não a substitui por uma busca indevida.
- Uma correção sobre a foto pedida anteriormente não é tratada como autorização para carrinho.
- O verificador independente recebe também URL, preço sob consulta, disponibilidade e características. Antes recebia apenas ID, nome e preço, podendo rejeitar respostas corretas por falta de evidência.
- A citação literal de uma observação de disponibilidade aceita pela camada de evidências é distinguida de uma afirmação de venda liberada.
- Promessas de transferência geradas sem autorização são convertidas em oferta; a recusa tem mensagem própria editável.

As migrações adicionais `20260916050127_regression_evidence_and_consent.sql` e `20260916051207_regression_purchase_availability.sql` foram aplicadas ao catálogo de configurações. As mensagens e regras são editáveis no banco; valores publicados específicos dos operadores não foram sobrescritos.

### Retomada exata

Usar o checkpoint privado `candidate-11-checkpoint.json` e as suítes `suite-v8.json`, `history-suite-v7.json`, `contact-suite-v6.json` e `contact-0859-suite-v5.json`. Há **524 valores de configuração congelados**. O histórico integral e os resultados permanecem na pasta ignorada `history-evaluation-regression/`.

1. Confirmar a recuperação da cota com um único caso, em uma nova pasta de resultados.
2. Executar os casos dirigidos e as sequências dos dois contatos; revisar as divergências restantes, especialmente seleção de variante, reclamações repetidas e contexto de mídia.
3. Reexecutar os 72 casos de desenvolvimento, os 24 replays gerais e os dois conjuntos de contatos no mesmo código e configuração.
4. Só após estabilizar, executar os 24 casos reservados e as duas repetições adicionais de cada caso crítico. Os casos reservados continuam sem uso.
5. Aplicar o cálculo de aprovação com denominador fixo, limites por categoria e zero erro crítico. Anexos recebidos, envio real por WhatsApp e transações reais continuam fora do escopo desta simulação.

O índice real foi novamente verificado: **zero produtos simulados**. As tabelas da regressão mantêm RLS e não concedem leitura direta a `anon` ou `authenticated`. Nenhum novo commit ou push foi realizado por esta continuação. A publicação paralela de consentimento em produção foi preservada.

## Continuação: consentimento confirmado e candidato 9

O usuário definiu **oferecer transferência e aguardar aceite**. O caso de reclamação agora testa também a recusa; o de avaliação de relógio usado testa o aceite no turno seguinte. O pedido explícito de um atendente continua autorizando a transferência. O avaliador precisa preservar o metadado da oferta para verificar esse fluxo.

Com o saldo restabelecido, o candidato 8 concluiu estes lotes:

| Conjunto | Aprovados | Reprovados | Total |
|---|---:|---:|---:|
| Desenvolvimento controlado | 66 (91,67%) | 6 | 72 |
| Contato final 8149 | 10 (55,56%) | 8 | 18 |
| Contato final 0859 | 9 (52,94%) | 8 | 17 |

**Esse resultado ainda não atende à campanha.** A taxa de desenvolvimento não representa a maturidade do produto. Os 24 cenários reservados não foram utilizados para ajustes. Cada sequência só passa quando todos os seus turnos passam; falhas antigas permanecem registradas.

Correções candidatas decorrentes dos resultados:

- Preservar pronta entrega durante a escolha de modelo e manter a família do relógio após nome e orçamento.
- Tratar interesse como consulta, sem criar carrinho; pesquisar uma nova família mesmo que já haja outros modelos da mesma marca na lista.
- Recuperar o produto mencionado para link e foto, consultando novamente o catálogo antes de responder.
- Retomar critérios conhecidos após uma reclamação; remover a família anterior ao pedir alternativas.
- Consultar todos os itens relevantes quando a pergunta sobre prazo é ambígua, sem selecionar arbitrariamente o primeiro.
- Diferenciar preço sob consulta de estoque e de liberação para compra. A observação textual de prazo não autoriza uma venda.
- Rejeitar caminhos de produto inventados mesmo em domínio oficial; manter uma resposta útil quando o catálogo não fornece link.
- Preservar as evidências de disponibilidade na validação e a oferta de transferência nos relatórios de avaliação.

As novas mensagens, expressões e famílias reconhecidas constam do catálogo do banco, com as migrações `20260916042135_regression_consent_and_context.sql` e `20260916044728_regression_context_repairs.sql`. Os valores candidatos são congelados nas suítes, sem sobrescrever os valores publicados pelos operadores.

As suítes do candidato 9 são `suite-v6.json` (96 casos), `history-suite-v5.json` (24), `contact-suite-v4.json` (18) e `contact-0859-suite-v3.json` (17), com 522 valores de configuração congelados. Seus resultados ficam na pasta privada ignorada.

Outra tarefa publicou em paralelo o commit `1f7d9c9` de consentimento. Essa publicação foi preservada; os candidatos desta campanha usam endereços individuais protegidos. Os checkpoints abaixo são registros anteriores e suas referências a quota ou produção não descrevem este estado mais recente.

## Atualização após reposição de saldo e inclusão do contato final 0859

A API voltou a responder, e a regressão foi retomada no candidato 6. Posteriormente, voltou a retornar `quota_exhausted`; o executor interrompeu novos cenários. **A campanha ainda não passou pelos critérios de conclusão.**

| Conjunto nesta continuação | Aprovados | Reprovados | Inconclusivos | Não iniciados |
|---|---:|---:|---:|---:|
| Desenvolvimento — 72 cenários | 52 | 6 | 3 | 11 |
| Contato final 8149 — 18 cenários | 9 | 9 | 0 | 0 |
| Contato final 0859 — 17 cenários | 6 | 7 | 1 | 3 |

O contato 0859 foi incluído com 23 pares históricos coletados, 17 cenários e 28 turnos previstos. Consulte [a análise e as evidências](ANALISE-CONTATO-0859.md).

As novas correções locais do candidato 7 tratam pronta entrega, prazo, recuperação de referência para mídia, contexto antigo na validação final, gênero e preservação de resultados válidos durante revisão. **2.122 testes locais passaram; 6 foram ignorados.** Houve três avisos de depreciação do SQLite. Os dez testes específicos também passaram após a última alteração de expressão de referência.

Sete regras/mensagens foram registradas e verificadas no banco. As evidências repetidas enviadas ao avaliador foram compactadas: redução medida de 22,03% nos caracteres de catálogo dos 126 turnos registrados, sem estimar economia financeira. O agente registrou 345 chamadas bem-sucedidas e 3.780.264 tokens nesta rodada; o consumo completo do avaliador não está incluído.

Não houve novo deployment, commit ou push nesta continuação. Os dois aliases públicos foram conferidos e permanecem em `dpl_GXg7ou4KXikGTpc5BEYssVQGrCjb`. O índice de catálogo continua com zero registros contendo URL de produto simulado. As correções do candidato 7 ainda precisam de avaliação com IA real.

O checkpoint privado `history-evaluation-regression/candidate-7-local-checkpoint.json` registra o código e os conjuntos para retomada. Começar pelos casos que falharam, em novas execuções, antes de repetir a campanha completa. Não retomar uma sequência cujo estado foi produzido durante a falha de cota. Os 24 cenários reservados continuam intocados; as repetições críticas e a meta acima de 90% permanecem pendentes.

## Checkpoint anterior, antes da reposição de saldo

**A campanha ainda não atingiu os critérios de conclusão.** Os logs da Vercel registraram HTTP 429 da OpenAI com a mensagem “You have no credits remaining”. A rodada do contato foi interrompida e permanece inconclusiva. A reposição de créditos da API é necessária para validar as correções com os modelos reais.

| Conjunto | Resultado medido | Observação |
|---|---:|---|
| Desenvolvimento controlado, candidato 5 | 61/72 — 84,72% | 8 áreas com o mesmo peso; 11 cenários falharam |
| Replay histórico, candidato 5 | 17/24 — 70,83% | Catálogo atual consultado em leitura |
| Validação reservada | 0/24 executados | Continua reservada; não usada para corrigir o agente |
| Contato final 8149 | 18 cenários / 35 turnos cadastrados | Rodada real inconclusiva por falta de créditos |
| Suíte automatizada local, candidato 6 | 2.111 aprovados / 6 ignorados | 3 avisos de depreciação do SQLite |
| Verificação adicional do cálculo de regressão | 13 testes aprovados | Inclui a exigência de repetições críticas |

No candidato 5, o tempo de processamento dos turnos controlados teve mediana de 13,22 segundos e p95 de 32,52 segundos. Foram registradas 220 chamadas bem-sucedidas do modelo do agente. Esse número **não inclui todo o consumo do avaliador nem equivale ao custo financeiro total**.

O índice de 84,72% refere-se somente ao lote de desenvolvimento executado. Não é a nota da campanha completa nem certificação do produto. As correções do candidato 6 ainda não têm resultado com IA real.

## Caso do contato

A coleta encontrou 110 entradas do contato. A campanha específica contempla:

- O incidente original da busca por automático com safira até R$ 2.500.
- Treze replays com o contexto real anterior, incluindo pedido de link, foto, endereço da loja e reclamação de indisponibilidade.
- Uma sequência com 12 mensagens reais, em que as respostas geradas e o estado produzido alimentam o turno seguinte.
- Quatro sequências controladas: consulta → link → foto; preço sob consulta → link → foto; indisponibilidade → alternativas; pedido explícito do endereço da loja.

Os resultados esperados foram definidos independentemente das respostas antigas. Interesse não autoriza confirmação de compra; preço ausente não comprova enquadramento no orçamento; estoque numérico não supera a indicação de indisponibilidade.

**Limite deste conjunto:** ele testa perguntas textuais sobre fotos e a recuperação de mídia oficial do catálogo. Não exercita a compreensão visual dos anexos enviados pelo cliente. Os problemas posteriores com imagens recebidas continuam pendentes de uma avaliação multimodal específica.

Os arquivos com histórico completo, identificadores, estado e respostas permanecem na pasta local ignorada `history-evaluation-regression/`. Eles não são incluídos no deploy, que exclui `docs`, `tests` e `scripts`.

## Correções preparadas no candidato 6

1. **Referência de produto sob consulta:** quando o agente menciona um produto identificado, mantém sua referência para link e foto, sem promovê-lo a opção com preço/disponibilidade confirmados.
2. **Inspeção e comparação:** a pergunta sobre uma característica de produtos conhecidos não vira filtro de uma nova recomendação. Uma comparação pode explicar corretamente que um item tem safira e outro usa Hardlex.
3. **Perguntas sobre produto conhecido:** consultas de dados e mídia passam antes dos atalhos de seleção de compra, preservando as proteções de ambiguidade e foto ausente.
4. **Correção de critérios:** negação de uma marca, correção de cor e nova busca com identidade antiga indevida retomam a pesquisa sem iniciar carrinho.
5. **Resposta útil com apresentação:** uma frase que se apresenta como Crono e responde a dimensão do produto deixa de ser descartada como mera saudação.
6. **Entrega e endereço da loja:** mensagens configuráveis orientam consulta de prazo sem contradizer a política de entrega inclusa; pedido explícito do site recebe o endereço configurado.
7. **Limite da API:** falta de saldo recebe uma categoria própria na avaliação; um erro 429 não provoca troca imediata para Chat Completions no mesmo provedor. A execução da campanha interrompe novas tarefas ao detectar esse bloqueio.
8. **Critério de conclusão:** o cálculo exige primeiro teste e duas repetições dos cenários críticos, pelo menos 95% de aprovação nas repetições e zero erro crítico. Repetições não aumentam a quantidade de cenários distintos.

As novas regras e mensagens constam de `agent_configuration_catalog`, com migração `20260916031606_regression_conversation_continuity.sql` no backend. Os valores candidatos estão nos conjuntos versionados no banco. **A configuração publicada da produção não foi substituída pelos overrides experimentais.**

## Isolamento verificado

Uma rodada anterior revelou escrita de nove produtos simulados no índice de catálogo. As nove linhas foram identificadas pelo endereço sintético, preservadas em evidência local e removidas. A causa foi o pooler não respeitar a opção de leitura enviada no início da conexão. A conexão de avaliação agora define `read_only` explicitamente; o caminho de indexação também recusa escrita durante avaliações.

A nova verificação encontrou **zero linhas com endereço de produto simulado** no índice. As tabelas `ai_regression_suites` e `ai_regression_runs` têm RLS habilitada e não permitem leitura direta aos papéis `anon` ou `authenticated`. Os endpoints de avaliação exigem autenticação administrativa.

## Retomada

O candidato 6 foi preparado no deployment `dpl_FT3GsT4rD4meBwRJJBqqDkRi3mBF`. Seu endereço individual é `ns-agent-for-sorteios-5ocreda24-newstores-projects.vercel.app`. Os aliases públicos permanecem apontando para o deployment anterior `ns-agent-for-sorteios-m0y2xhftu-newstores-projects.vercel.app`.

O arquivo privado `history-evaluation-regression/candidate-6-checkpoint.json` registra o hash dos arquivos de aplicação e os identificadores/fingerprints das suítes. Usar `suite-v4.json`, `history-suite-v3.json` e `contact-suite-v2.json` na retomada.

Ordem restante:

1. Confirmar que o saldo da API foi restabelecido com uma execução pequena.
2. Executar o contato em uma nova pasta de resultados. Não continuar sequências cujo contexto foi produzido durante a indisponibilidade do modelo.
3. Reexecutar desenvolvimento e histórico no mesmo candidato; investigar as divergências restantes, incluindo pedido pendente, escopo de ausência de produto, recuperação e instruções indevidas.
4. Ao estabilizar, executar os 24 casos reservados e as repetições críticas, preservando o denominador e todas as falhas.
5. Validar anexos recebidos e a entrega real da resposta no canal; o simulador não comprova envio por WhatsApp nem transação real no provedor.
6. Consolidar os resultados e somente então publicar a configuração validada e avaliar a meta acima de 90%.

Nenhum push foi feito nesta continuação.
