# Regressão conversacional e critério de prontidão

## Escopo e meta definidos antes das execuções

A meta desta rodada é superar 90% em um índice de prontidão do **escopo conversacional testado**. Não é uma certificação de todo o produto. Os testes unitários existentes são uma condição de entrada e não aumentam artificialmente a nota das conversas.

Matriz de oito áreas, com o mesmo peso:

1. Busca, filtros e resultados do catálogo.
2. Características e fidelidade das respostas aos dados.
3. Memória e continuidade em múltiplos turnos.
4. Carrinho, finalização, pagamento e acompanhamento.
5. Recuperação de erros, esclarecimento e atendimento humano.
6. Persona, políticas comerciais e configuração dinâmica.
7. Indisponibilidade de integrações e dados incompletos.
8. Isolamento, privacidade e resistência a instruções indevidas.

O índice é a média das taxas de aprovação das oito áreas. Um cenário só passa quando todos os seus turnos cumprem os resultados esperados. Erros de execução, casos não executados e resultados inconclusivos permanecem no denominador. Repetições não contam como cenários novos.

Critérios de conclusão:

- Índice estritamente acima de 90%, sem área abaixo de 85%.
- Pelo menos dez cenários distintos por área.
- Zero falha crítica de autorização, privacidade, integridade monetária ou confirmação indevida de pagamento/pedido.
- Lote reservado de validação com pelo menos 20 cenários e aprovação acima de 90%.
- Repetição dos casos críticos, com pelo menos 95% de aprovação.
- Testes automatizados existentes aprovados; erros novos e limitações explicitamente registrados.

Tempo de resposta, chamadas, tokens e falhas de integração são relatados separadamente. Uma resposta correta que demora demais deve aparecer como limitação operacional, mesmo quando passa no critério funcional.

## Dados e resultados esperados

A coleta inicial encontrou 645 turnos entregues em 243 conversas. A base preserva empresa, canal, conta e contexto anterior. Respostas antigas servem como evidência de comportamento, nunca como gabarito automático.

Os cenários combinam casos históricos, variações de linguagem e sequências controladas. Cada turno possui critérios semânticos e verificações objetivas de consultas, estado, resultados e operações proibidas. Os critérios são versionados no banco antes da execução. Alterá-los após encontrar uma falha exige registrar a justificativa e criar nova versão; não se relaxam critérios para melhorar a nota.

As conversas reservadas para validação ficam separadas dos exemplos usados nas correções. Se uma falha desse lote for usada no ajuste, ela passa a integrar a regressão de desenvolvimento e um novo lote reservado precisa ser selecionado.

## Execução e isolamento

O modelo é real, usando a persona e a configuração publicadas. O histórico e o estado produzidos pelo agente alimentam os turnos seguintes. Cada turno é persistido separadamente, permitindo continuar sem reenviar ações após uma interrupção.

Consultas históricas usam o catálogo atual em modo de leitura. Fluxos com operações comerciais usam um simulador em memória: carrinhos, pedidos, frete e pagamento são dados de teste. O simulador nunca autoriza uma chamada de escrita à loja ou ao provedor financeiro. Seus resultados validam decisões e contratos de integração; não comprovam uma transação real no provedor.

Os resultados distinguem explicitamente modelo real, ferramentas reais de leitura, ferramentas simuladas e verificações determinísticas. A aprovação do avaliador por IA precisa ser compatível com os dados e com as verificações objetivas.

Referência metodológica: [boas práticas de avaliações da OpenAI](https://developers.openai.com/api/docs/guides/evaluation-best-practices), para objetivos específicos, dados representativos, critérios prévios e calibração do avaliador.
