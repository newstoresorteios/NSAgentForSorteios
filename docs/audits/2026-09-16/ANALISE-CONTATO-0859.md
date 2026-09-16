# Conversa do contato final 0859 — análise e regressão

## Conclusão

A conversa apresenta dois grupos de problemas: perda de critérios e referência do produto, além de falhas do provedor de IA. Repor créditos permitiu retomar os testes, mas não corrigiu os problemas de continuidade. A nova rodada voltou a registrar `quota_exhausted`, interrompendo a validação com modelos reais.

Foram coletados **23 pares de entrada e resposta entregue** do contato, com o contexto, o estado e os registros de execução associados. O conjunto foi registrado no banco como `contato-0859-regressao`, versão 1: **17 cenários e 28 turnos previstos**. Os dados pessoais e o histórico integral ficam em arquivos locais ignorados pelo Git.

## Evidências da conversa

| Referência da resposta | Pedido e comportamento observado | Diagnóstico |
|---|---|---|
| 640–643 | Baltic a pronta entrega → MK2 → cinza de 37 mm. O atendimento passou a oferecer Hermétique Tourer de outras cores. | A linha solicitada e os critérios não permaneceram obrigatórios ao refinar a busca. |
| 752, 754, 755 | Longines Heritage preto → nome do cliente → orçamento de R$ 20 mil. A resposta posterior trouxe Hydroconquest. | A coleta de nome/orçamento perdeu a identidade do pedido anterior. |
| 808 | Certina a pronta entrega. A resposta apresentou três produtos sem comprovar disponibilidade imediata. | O registro contém seis falhas de chamadas OpenAI; a interpretação de contingência preservou a marca e perdeu pronta entrega. As 17 chamadas Tray registradas foram bem-sucedidas. |
| 809 | “Esse modelo é pronta entrega ou sob encomenda?” recebeu falha genérica de consulta. | A interpretação de contingência usou a própria pergunta como nome do modelo. As nove chamadas Tray registradas tiveram sucesso, enquanto seis chamadas OpenAI falharam. |
| 810 | “Qual é o prazo de entrega?” recebeu encaminhamento genérico. | Quatro chamadas OpenAI falharam; nenhuma chamada Tray foi feita nesse turno. Havia contexto útil que poderia orientar a resposta. |

Os três últimos registros ocorreram em 16/09/2026, entre 03:04 e 03:07 UTC. Os metadados demonstram falhas do modelo; não sustentam atribuir esses turnos a indisponibilidade do Tray/Render. Não foi feita uma nova investigação geral da infraestrutura Render nesta continuação.

## Resultado da nova rodada

Executada no candidato 6, antes das correções locais descritas abaixo:

- 6 cenários aprovados, 7 reprovados, 1 inconclusivo por cota e 3 não iniciados.
- 21 turnos registrados: 9 aprovados, 11 reprovados e 1 inconclusivo.
- A sequência Longines ainda perdeu continuidade; pronta entrega e prazo ainda produziram respostas inadequadas.
- O avaliador identificou perguntas redundantes sobre dados já informados, inclusive cor e tamanho.

**Esses resultados não aprovam o conjunto.** Os casos inconclusivos e pendentes continuam no denominador. A resposta antiga serve como evidência do problema, não como resultado esperado correto.

## Correções locais preparadas

1. Preservar a exigência de pronta entrega ao refinar o modelo e permitir removê-la quando o cliente aceita encomenda.
2. Filtrar recomendações por evidência de pronta entrega, sem usar apenas o estoque numérico. A categoria que caracteriza pronta entrega passou a vir do banco.
3. Consultar a ficha dos produtos já apresentados para responder sobre prazo, sem transformar uma pergunta em seleção de compra ou nome de modelo.
4. Recuperar uma única referência mencionada no histórico para pedidos de link/foto, confirmando-a no catálogo. Referências ambíguas não são escolhidas automaticamente; dimensões como `42mm` não são tratadas como código de produto.
5. Usar a interpretação corrigida na validação final, evitando reintroduzir a marca da recomendação anterior após uma correção de cor.
6. Evitar inferir uma família comum a partir de apenas um produto de uma lista e remover a família antiga quando o cliente muda de modelo.
7. Preservar produtos já verificados que atendem ao pedido quando uma busca suplementar retorna vazia; aplicar o gênero solicitado antes de recomendar.

Essas mudanças passaram na suíte local: **2.122 testes aprovados e 6 ignorados**, com três avisos de depreciação do SQLite. A última alteração de expressão de referência também passou nos dez testes específicos. Isso comprova os contratos testados localmente, não substitui a próxima rodada com IA real.

## Configuração e consumo

Sete definições de regras/mensagens foram gravadas em `agent_configuration_catalog` e comparadas com o seed local. Categoria de pronta entrega, critérios de continuidade, recuperação de referência e textos de resposta são editáveis por configuração. Nenhuma credencial foi incluída nesses registros.

O avaliador passou a referenciar fichas repetidas sem reenviar cópias idênticas. Em 126 turnos registrados nesta continuação, o volume de caracteres das evidências de catálogo caiu **22,03%**, preservando preços divergentes e URLs de fotos. Também foram removidas informações de configuração e diagnóstico duplicadas do pedido ao avaliador. Essa medida ainda precisa de validação semântica com modelo real; não equivale a uma redução medida de 22,03% na fatura.

Os registros do agente somam 345 chamadas bem-sucedidas e 3.780.264 tokens de entrada e saída nessa rodada, incluindo tokens de entrada em cache. O total não inclui todo o consumo do avaliador e não permite calcular a cobrança final.

## Pendências

As correções do candidato 7 estão locais, sem deployment novo, commit ou push. Os aliases públicos continuam no deployment anterior e os overrides experimentais não foram publicados como configuração de produção.

Com a API disponível, executar primeiro os casos que reproduzem cada falha; depois, o desenvolvimento completo, os dois contatos e o replay histórico no mesmo candidato/configuração. Em seguida, executar os 24 casos reservados e as repetições críticas. A meta acima de 90% permanece não comprovada. Recebimento de imagens, envio real pelo WhatsApp e transações reais continuam fora desta validação.
