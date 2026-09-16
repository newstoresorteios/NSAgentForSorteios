# Avaliação do agente com conversas reais

## Funcionamento

1. Importa mensagens com respostas efetivamente entregues, limitadas à empresa, canal e conta de origem.
2. Reconstrói o contexto anterior a cada mensagem. A resposta que será avaliada nunca entra no contexto do agente.
3. Executa o pipeline completo com a persona e configuração publicadas. O modelo usa sua credencial do servidor.
4. Consulta o catálogo atual em modo de leitura e registra ferramentas, argumentos, resultados, decisões, revisões, tempos e uso do modelo.
5. Outro chamado ao modelo compara o pedido com as evidências e classifica a resposta. Verificações de execução impedem aprovar erros conhecidos ou respostas rejeitadas sem correção.
6. Quando habilitado, testa um pequeno complemento de instrução em memória, reutilizando os resultados das ferramentas. Essa proposta não altera a persona publicada.

Os casos e execuções ficam nas tabelas `ai_conversation_evaluation_cases` e `ai_conversation_evaluation_runs`. O painel de aprendizado do ChatBo exibe as execuções recentes da empresa autenticada.

## Executar

Use um arquivo JSON local privado contendo a propriedade `token`, com o token administrativo da instalação testada. Não adicione esse arquivo nem relatórios com conversas ao Git.

```powershell
python scripts/evaluate_history.py `
  --workspace <uuid-da-empresa> `
  --base-url <url-do-agente-em-teste> `
  --access-file <arquivo-privado.json> `
  --output <pasta-local-de-relatorios> `
  --limit 20 `
  --no-repair
```

Para repetir casos já importados, use `--case-id <uuid>` uma ou mais vezes. Remova `--no-repair` para testar propostas de instrução. Instalações protegidas pela Vercel também aceitam `--vercel-cli <vc.js>` e `--vercel-auth-dir <pasta-autenticada>`.

O código de saída é 0 para lote aprovado, 1 para divergência/inconclusivo e 2 para erro de execução. O manifesto grava cada identificador de execução antes da chamada; em caso de interrupção, consulte a execução salva antes de iniciar outra.

APIs administrativas:

- `POST /api/admin/evaluations/import`: importa casos da empresa.
- `POST /api/admin/evaluations/run`: executa um caso com identificador de requisição idempotente.
- `GET /api/admin/evaluations/{workspace_id}`: consulta casos e resultados.

O endpoint do painel é `GET /agent-learning/evaluations`, no backend ChatBo, com a permissão `managePlatform` e a empresa derivada da sessão autenticada.

## Configuração pelos operadores

Os limites, modelo avaliador, instruções do avaliador e proposta de correção são entradas `historyEvaluation*` e `message.history_evaluation_judge` do catálogo no banco. Os textos de disponibilidade, contrato de resposta após consulta e regras para perguntas sobre produtos também são entradas editáveis no banco.

As permissões que impedem pagamentos, pedidos, carrinhos e envio de mensagens em simulações são uma barreira do código. Não são permissões de negócio que um operador possa habilitar.

## Leitura dos resultados

- **Aprovado:** o caso atendeu aos critérios da avaliação executada.
- **Divergência:** há evidência de uma falha de decisão ou resposta. Examine a primeira etapa responsável antes de alterar prompts.
- **Inconclusivo:** faltam evidências ou a infraestrutura/simulação não permitiu concluir o teste.
- **Fluxo determinístico:** o caso pode estar correto, mas não exercitou geração pelo modelo.
- **Proposta verificada:** passou naquele caso com as mesmas respostas das ferramentas; ainda exige avaliação de regressão antes de publicação.

As versões incluem persona, configuração, hash dos valores, caso, modelo e deployment. Resultados de deployments e configurações diferentes não devem ser agregados como se fossem uma única versão.

## Isolamento e limites

Cada execução usa uma conversa sintética, memória local e conexão de banco somente para leitura durante a simulação. As gravações de casos e relatórios acontecem fora desse contexto. Ferramentas de escrita e os canais de saída são bloqueados antes da rede.

O prazo de validade da memória preserva a idade que tinha na mensagem original. Isso evita expirar artificialmente uma lista de produtos apenas porque a avaliação ocorreu dias depois.

Esta é uma reexecução de turnos com o histórico original, e não uma reprodução exata de preços e estoque antigos nem uma conversa autônoma inteira entre dois modelos. O catálogo consultado é o atual. Históricos podem estar incompletos; respostas não entregues não são usadas como fala anterior do agente.

Um avaliador por IA também pode errar. Mantenha os exemplos de falhas como regressões, revise as evidências e repita casos críticos. Uma amostra aprovada não garante todas as conversas futuras.

## Correções e nova avaliação

Uma divergência de código ou integração deve ser corrigida na etapa indicada pelo rastreio e ganhar uma regressão que reproduza o erro. Depois, repita o caso e os casos relacionados no deployment candidato. Uma divergência apenas de instrução pode gerar uma proposta testada em memória pelo avaliador. A aprovação de um caso não publica automaticamente uma nova persona nem modifica políticas comerciais.

O mecanismo executa lotes sob demanda por API/CLI; o painel consulta os resultados. Não há agendamento recorrente nem treinamento automático de pesos do modelo. A reexecução pode consultar preços e estoque atuais, mas nunca deve alterar o catálogo para fazer um teste passar.
