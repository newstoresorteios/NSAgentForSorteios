# Correções da auditoria do NSAgent

Execução em 01/10/2026, horário de Brasília. Escopo: NSAgent; BI excluído.

## Código e publicação

- `aa6d8ee`: retirou a dependência catálogo → vendas. O tratamento de prints de suporte agora ocorre na orquestração da conversa, preservando o comportamento. O teste de histórico valida a identificação da IA e a recuperação da resposta efetivamente persistida.
- `655242d`: vinculou os agendamentos ao único workspace ativo do NSAgent para a persona configurada. O aprendizado filtra respostas e revisões por workspace; a leitura de revisões aceita as linhas em formato de dicionário usadas em produção. O remarketing recupera o provedor da mensagem original e envia pela Meta quando essa é a origem.
- `fd33271`: passou a gravar o workspace nos registros novos de remarketing, recusando a reutilização de um contato de outro workspace. Excluiu o cache local de ferramentas do Git, do deploy e do pacote de distribuição.

Foram criados três Deployment Checks na Vercel, obrigatórios para atribuição dos domínios de produção: `quality`, `offline-eval` e `package-only`. O auto-assignment de produção permanece ativo. A proteção foi observada em funcionamento: o build de `aa6d8ee` terminou enquanto o domínio ainda servia `64615f2`; a promoção ocorreu após os checks. Não houve promoção forçada.

CI aprovada para [aa6d8ee](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36810850853), [655242d](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36811629057) e [fd33271](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36812043501). O health confirmou `fd33271a9810` em produção.

## Agendamentos e aprendizado

`AGENT_BASE_URL` e `CRON_SECRET` foram configurados no GitHub. O segredo do cron foi renovado e sincronizado com a variável sensível de produção na Vercel; valores não são incluídos neste relatório. A primeira chamada autenticada revelou um segundo impedimento, além dos secrets ausentes: `ambiguous_persona_workspace`, seguido de `Missing published policy: business.agent_name`. A vinculação explícita do workspace corrigiu esse erro.

Após o deploy, o endpoint retornou HTTP 200 e `ok=true`: 168 respostas examinadas, cursor **778 → 1063**, 66 conversas. A configuração publicada já permitia promoção e ativação automáticas; essa execução registrou cinco insights e cinco extensões ativadas. O workflow [Attendance Learning](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36811931667), disparado pelo GitHub, também concluiu com sucesso. O cursor registrou nova execução às 03:45:01 UTC.

## Remarketing

A revisão encontrou 46 tentativas pendentes, todas vinculadas a mensagens da New Store recebidas pela Meta. Os campos de workspace das tabelas de remarketing estavam nulos; por isso a consulta inicial da auditoria, filtrada diretamente por esse campo, não as contabilizou. A atribuição foi recuperada usando a mensagem de origem: 122 contatos com workspace inequívoco, 264 ciclos de conversa e 792 tentativas históricas receberam o workspace confirmado.

Havia 12 primeiras tentativas vencidas, de 12 contatos distintos, ainda dentro da janela de envio. A retomada não altera consentimento, opt-out, cancelamentos, transferência humana ou janela de 24 horas. O código agora filtra o workspace inclusive na recuperação de leases e na expiração, e preserva o provedor de origem. O workflow foi pausado durante a correção para impedir a execução do caminho antigo.

Após a aprovação da CI e a confirmação da versão final em produção, o workflow de remarketing foi reativado, com estado `active` confirmado pela API. Não foi disparado um lote manual de mensagens; a próxima execução segue o agendamento existente de 15 minutos. A entrega posterior à correção ainda não foi observada.

## Stories e referências

O vídeo arquivado foi reprocessado em produção no job **9**, sem recolocar a mensagem do cliente na fila de atendimento. Resultado persistido: `story-worker-v5`, status `ready`, uma tentativa, nove candidatos e **zero identidades exatas aprovadas**. O processamento levou aproximadamente dois minutos e terminou às 03:43:38 UTC. Isso comprova a execução da versão atual, mas não comprova identificação correta do relógio.

A base de referências dos Destaques continua dependendo dos nomes e links oficiais confirmados pelo operador. A informação foi solicitada; não foram cadastradas hipóteses como produtos confirmados. A validação de identificação contra referências conhecidas e de resposta útil em conversa real posterior permanece pendente. A suíte cobre seleção entre vários relógios, “o Mido”, preço, foto posterior e ausência de correspondência com dependências simuladas.

## Validação e limites

A primeira correção passou em 2.797 testes, sete ignorados, cobertura de 75,36%; 102 avaliações offline passaram. As alterações dos agendamentos passaram na CI remota e em 76 testes locais de aprendizado, empacotamento e segredos. Uma execução local encontrou falta de espaço no disco C: em três testes com arquivos de 100 MiB; os 35 testes do módulo passaram ao usar temporários no disco D:. Outra execução detectou fixtures de teste dentro do cache local; o cache foi excluído dos artefatos de distribuição, e o scanner passou novamente.

O health público ainda informa um aviso de configuração. Sua causa não foi determinada por diagnóstico administrativo, pois o token administrativo existente é uma variável sensível não recuperável pela API. Ele não foi substituído para obter acesso.

A execução local final terminou com **2.803 testes aprovados, sete ignorados e nenhuma falha**. Os três jobs da CI final (`quality`, `offline-eval`, `package-only`) também passaram. Evidência local: `.pytest-audit-final-clean.txt`; cobertura medida nas execuções desta correção: 75,36%, acima do mínimo de 70%.
