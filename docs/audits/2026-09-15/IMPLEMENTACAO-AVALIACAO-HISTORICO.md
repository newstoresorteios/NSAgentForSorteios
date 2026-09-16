# Avaliação do NSAgent com histórico real

## Entrega

Foi implementada a importação de turnos com respostas efetivamente entregues, reconstrução do contexto anterior, reexecução do pipeline com o modelo real, avaliação por IA e registro das consultas e decisões. A resposta histórica não é usada como gabarito nem apresentada ao agente durante a reexecução.

O ChatBo recebeu um painel em **Aprendizado → Avaliações com conversas reais**, com resposta antiga e atual, achados, consultas, versões e propostas. Os lotes são executados por API/CLI, conforme o [guia de operação](../../AVALIACAO-HISTORICO.md). Não foi criado agendamento recorrente.

As novas mensagens, instruções de avaliação e políticas de negócio estão no catálogo do banco. Limites de segurança que impedem pagamentos, carrinhos e mensagens reais em uma simulação permanecem no código.

## Divergências corrigidas

1. **Pergunta confundida com compra:** a referência numérica em uma pergunta sobre um produto não inicia carrinho. A inspeção conserva a identidade do produto, mesmo quando a característica perguntada não existe nele.
2. **Disponibilidade mal interpretada:** estoque numérico não sobrepõe os campos comerciais. A observação de prazo pode ser citada sem prometer venda ou entrega; prazo isolado não prova encomenda.
3. **Resultado da consulta perdido:** o compositor e o revisor recebem evidência técnica e comercial, inclusive quando nenhum produto pode ser oferecido. A resposta de contingência preserva a limitação concreta encontrada.
4. **Movimento automático descartado:** corda manual auxiliar é compatível com automático quando ambos estão confirmados; isso não transforma um movimento apenas manual em automático.
5. **Erro 400 do modelo:** o schema de propostas de memória foi corrigido para saída estruturada estrita, preservando a representação interna existente.
6. **Benefícios sem fonte:** as instruções do compositor e do revisor passaram a vedar afirmações específicas de manutenção e outros benefícios sem evidência do produto.
7. **Simulação com memória expirada:** a reexecução conserva a idade da memória no instante original da mensagem, evitando resultados distorcidos por testar uma conversa antiga hoje.

## Validação técnica

- NSAgent: **2.079 testes aprovados, 6 ignorados**, com 3 avisos existentes.
- Backend: **165 testes aprovados**, com avisos de depreciação existentes.
- Frontend: compilação TypeScript e build Vite concluídos; permanece o aviso de tamanho de bundle.
- Tabelas de avaliação com RLS e sem acesso direto dos papéis `anon` e `authenticated`.
- Consulta ao banco confirmou **zero mensagens sintéticas** nas conversas de clientes.
- Sete migrações de avaliação e configuração aplicadas. Catálogo com 499 entradas após a implementação.

## Publicação e limites

O código foi validado antes do push em um deployment candidato protegido, `dpl_E1khmtC82wV81LLnyvFwLJ6AyoUT`. Durante a avaliação, os endereços principais permaneceram no deployment anterior. As migrações e definições do catálogo foram aplicadas no banco antes da publicação do código. O estado de publicação posterior deve ser conferido pelo commit e pelo deployment de cada serviço.

O SKU **9795**, Orient Mako Kamasu III RA-AA0820R19B, confirma automático e safira, mas a integração retornou preço zero e `upon_request=1`. O agente deve explicar preço e disponibilidade sob consulta, sem confirmar o teto de R$ 2.500. O preço comercial não foi inventado nem alterado.

A amostra usa seis turnos históricos, com duas saudações determinísticas. Não representa todas as conversas nem cobre compras reais. O catálogo é atual; os históricos podem estar incompletos. Aprovações antigas foram preservadas com suas versões, inclusive avaliações excessivamente permissivas identificadas durante a revisão. Uma nota positiva da IA não substitui a leitura das evidências.

Os relatórios brutos permanecem locais e no banco, fora do Git, pois contêm conversas. Os resultados da última configuração estão registrados abaixo.

## Resultado da rodada final

**6/6 turnos aprovados**, sendo quatro com geração pelo modelo e duas saudações determinísticas. Duas repetições adicionais da pergunta sobre o movimento também passaram. Nenhuma destas oito execuções registrou ferramenta bloqueada ou erro de integração.

| Caso | Resultado observado | Execução |
| --- | --- | --- |
| Característica do primeiro produto | Identificou o movimento quartzo e não iniciou carrinho | `3d357b4a-6efa-4e1f-ac89-458b9f19ce2e` |
| Automático e safira até R$ 2.500 | Identificou o modelo compatível e explicou preço e disponibilidade sob consulta | `eeb7cd08-ef0b-4aad-97c1-4f7136f46f41` |
| Cliente reclama de incompreensão | Retomou o contexto, explicou indisponibilidade e citou a observação da ficha | `d3b4e697-8f72-4a6b-9861-201ade44c90f` |
| Saudação A | Usou a saudação publicada da persona | `03cf1fe7-6eb3-4313-bd72-954f19ec1f9a` |
| Pedido genérico de relógio | Apresentou três produtos do catálogo com preços e links | `9facb538-709d-4bac-9996-7c7fa5ff3216` |
| Saudação B | Usou a saudação publicada da persona | `99a64979-b829-4060-93e7-d75ffd3883b3` |

Repetições aprovadas: `777932f6-eb88-40a4-8ef1-c9826323069c` e `9c3c40df-25cd-439e-8701-12e31ce359e5`.

Modelo do agente: `gpt-5.4-mini`; persona 17; configuração 1; hash efetivo `57459da727121b54d396a9fd31bd55f46e3e146ff9d4630d43afcad3287387fb`. O hash distingue as mudanças de definições do catálogo mesmo quando o número de versão do workspace permanece igual.

A correção de instrução sobre benefícios foi aplicada no banco e verificada no mesmo deployment, demonstrando atualização dinâmica sem recompilar o agente. A rotina também permite testar propostas em memória com resultados de ferramentas congelados, mas nenhuma proposta precisou ser promovida na rodada final. Os testes de regressão cobrem esse mecanismo; não houve treinamento de modelo nem alteração automática da persona publicada.
