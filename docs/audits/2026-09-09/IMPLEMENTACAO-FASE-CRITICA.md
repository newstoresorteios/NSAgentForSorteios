# Implementação da fase crítica da auditoria v100

Data: 9 de setembro de 2026
Base auditada: `7cb761f`

## Resultado

Este lote trata os riscos críticos e altos que podiam causar aprendizado com dados falsos, perda de contexto em consultas simples, excesso de chamadas ao Tray Adaptor, disparos tardios de remarketing e processamento sem limite de payload.

## Correções entregues

### DR-01 — integridade do aprendizado

- O coletor seleciona somente respostas efetivamente entregues, com texto não vazio.
- Registros `skipped` e `dry_run` são excluídos.
- Para cada mensagem recebida, somente a entrega válida mais recente é usada como evidência.
- Uma interação já revisada por meio de uma entrega válida não volta a ser aprendida por causa de uma duplicata posterior.
- Os testes cobrem duas falhas seguidas de sucesso, duplicidade de sucessos, ausência de sucesso, `skipped`, `dry_run` e revisão anterior.

### DR-02 e DR-06 — contexto e conhecimento institucional

- O contrato extenso de checkout só entra em turnos de compra, carrinho, pagamento ou checkout.
- Consultas comuns de catálogo usam o contrato comercial reduzido.
- Anexos institucionais são divididos em blocos limitados e selecionados por relevância lexical para a mensagem atual.
- Cada trecho recuperado identifica a fonte e o bloco, por exemplo `source:<id>#chunk-<n>`.
- Conteúdo de anexo historicamente anexado ao texto da persona é removido durante a compilação para evitar duplicação.
- Itens institucionais configurados na persona só entram quando possuem relação com a pergunta atual.
- A telemetria do compilador passa a registrar a quantidade de caracteres de cada camada do prompt.

Na persona observada durante a auditoria, a separação do apêndice reduziu o texto-base de 24.335 para 15.841 caracteres, uma redução de 8.494 caracteres antes da recuperação seletiva.

### DR-03 e DR-12 — busca adaptativa e diversidade

- Buscas exatas priorizam EAN, referência e código de modelo.
- Uma correspondência exata encerra a busca sem disparar probes especulativos.
- Recomendações consultam o catálogo em lotes pequenos e param quando já há evidência suficiente.
- Pedidos como “outras marcas” ou “uma de cada marca” somente encerram cedo quando atingem diversidade real de marcas.
- O orçamento padrão é de até seis consultas de listagem por recuperação, configurável separadamente para busca exata e recomendação.
- A busca por cor preserva parte do orçamento para páginas e consultas específicas de variante.
- O preenchimento de cache de marca caiu de 12 para 4 páginas por falta e passou a respeitar o orçamento da sessão.

### DR-04 e DR-10 — dependências e payload HTTP

- FastAPI, Starlette, python-dotenv e python-multipart foram atualizados para versões auditadas.
- Webhooks aceitam no máximo 1 MiB de corpo, inclusive quando a requisição usa transferência em blocos e não informa `Content-Length`.
- Corpos Meta são limitados antes da verificação HMAC e os bytes exatos continuam sendo usados na assinatura.
- Formulários Brevo permanecem parseáveis após a leitura limitada.
- `pip-audit` não encontrou vulnerabilidades conhecidas nas dependências fixadas.

### DR-05 — remarketing e outbox

- O worker de remarketing revalida o lease, o ciclo ativo, a elegibilidade, a janela de mensagens e o horário da última mensagem do cliente antes do envio.
- Após uma consulta lenta à Tray, a condição é validada novamente para impedir que um lembrete cancelado use uma cópia antiga já reivindicada pelo worker.
- A identidade temporal do lease impede que um worker antigo use uma tentativa recuperada por outro worker.
- Os testes cobrem resposta do cliente após o claim, resposta durante consulta à Tray, novo ciclo, opt-out, recuperação do outbox, auditoria sem duplicidade, dead letter e descarte de resposta atrasada após nova entrada.

## Validação executada

| Verificação | Resultado |
|---|---:|
| Suíte completa | 1.934 aprovados, 1 ignorado |
| Backtests `offline_eval` | 101 aprovados |
| Cobertura total com branches | 73% |
| Cobertura de `retrieval/probes.py` | 91% |
| Cobertura de `prompt_compiler.py` | 81% |
| Cobertura de `persona_knowledge_repository.py` | 81% |
| Compilação de `app/` e `api/` | aprovada |
| Ruff nos arquivos alterados | aprovado |
| `pip-audit` | nenhuma vulnerabilidade conhecida |

O lint global ainda encontra dívida histórica em arquivos fora deste lote. Os arquivos alterados e os novos testes passam sem erros no Ruff.

## Pontos que exigem medição após publicação

- Comparar chamadas ao Tray, chamadas ao banco, tokens de entrada e latência com a conversa-base da auditoria.
- Confirmar em conversa real a continuidade após “outras marcas”, refinamento de preço/cor e pedido de foto.
- Observar se o remarketing deixa de emitir mensagens após uma resposta recente do cliente.
- Usar `layer_char_counts` para identificar as camadas que ainda dominam o prompt.
- Avaliar uma segunda fase de recuperação semântica por embeddings quando houver um conjunto maior de documentos e perguntas reais rotuladas; o lote atual já reduz contexto e adiciona evidência rastreável sem criar uma chamada extra ao modelo.
- Reduzir as leituras repetidas de banco por turno e consolidar validadores que ainda competem pelo orçamento de LLM.
