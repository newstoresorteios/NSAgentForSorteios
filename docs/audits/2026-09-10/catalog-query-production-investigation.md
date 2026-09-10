# Investigação de produção: consulta automática, safira, open heart ou skeleton

Data da análise: 10/09/2026  
Contato analisado: `5585999498149`  
Inbound: `784`  
Trace: `669aadc6a87c45e8ad61146a2b8e08cf`

## Resultado

A resposta genérica não foi causada por falta de capacidade do modelo. A consulta perdeu a semântica antes da apresentação da resposta:

1. `open heart ou skeleton` virou o modelo exato duplicado `Open Heart Open Heart`;
2. a marca `Hamilton` e o shortlist da conversa anterior vazaram para a nova busca sem marca;
3. o agente enviou `current_price_range=0,3500` ao TrayAdaptor;
4. a API da Tray recusou esse parâmetro e o agente repetiu a busca sem teto;
5. produtos Hamilton de R$ 9.399,99 e R$ 10.199,99 chegaram ao conselho de resposta;
6. o conselho bloqueou corretamente os itens acima do orçamento;
7. o double-check classificou a resposta segura de ausência como `unanswered` e a substituiu pela mensagem genérica.

O índice do Supabase não contém, neste momento, produto disponível até R$ 3.500 que confirme simultaneamente automático, safira e open heart/skeleton. Assim, o comportamento correto é informar a ausência exata, preservar os critérios e oferecer relaxamento de um critério por vez.

## Evidência cruzada

### Vercel

O trace teve 33,263 segundos, 19 chamadas à Tray, 69 chamadas ao banco, cinco tentativas de transporte OpenAI e 24.889 tokens de entrada. O contrato da resposta registrou `brand=hamilton`, `model=Open Heart Open Heart`, `brand_lock`, `model_lock` e `sku_lock`, embora a mensagem atual não mencionasse marca nem SKU. O conselho rejeitou a resposta por `presented_over_budget` e `fact_price_over_budget`; em seguida, o double-check aplicou `double_check_insufficient`.

Também foram encontrados dois erros operacionais independentes:

- `tray_keepalive_cron` falhava ao acessar `urllib.urlparse`;
- `tray_sync_cron` falhava porque `public.ai_tray_sync_cursors` não existia em produção.

### Render e TrayAdaptor

O TrayAdaptor recebeu seis respostas 500 em três pares de tentativas:

- `name=Open Heart Open Heart&current_price_range=0,3500`;
- `name=Relógio Open Heart&current_price_range=0,3500`;
- `brand=hamilton&model=Open Heart Open Heart&current_price_range=0,3500`.

As mesmas consultas sem `current_price_range` retornaram 200, demonstrando que o filtro era o gatilho. CPU e memória permaneceram normais, descartando pressão de recursos. A documentação oficial da Tray lista `price_range` como parâmetro de consulta de produtos; `current_price_range` aparece como filtro disponível na resposta, mas não na lista de parâmetros de requisição. Fonte: [API de Produtos da Tray](https://developers.tray.com.br/).

### Supabase

Os produtos recuperados no trace foram:

| ID | Produto | Preço | Disponível |
|---|---|---:|---|
| 1045 | Hamilton Jazzmaster Open Heart H32705181 | R$ 10.199,99 | sim |
| 1403 | Hamilton Jazzmaster Open Heart H32705041 | R$ 10.199,99 | sim |
| 1535 | Hamilton Jazzmaster Open Heart H32705121 | R$ 9.399,99 | sim |

A consulta conjunta no índice, exigindo disponibilidade, preço até R$ 3.500, movimento automático, safira e um dos dois estilos, retornou zero linhas. A tabela de cursor ausente foi criada por migração idempotente.

### Conversas recentes

O mesmo padrão apareceu em outros turnos:

- inbound 780, contato `5549991568669`: uma resposta de qualificação após orçamento/nome foi substituída por `double_check_insufficient`;
- inbound 745, contato `5585999498149`: uma ausência dentro da faixa também virou resposta genérica;
- inbounds 776 e 752: a indisponibilidade do Orient foi tratada de modo informativo, mostrando que o caminho determinístico de produto indisponível funciona;
- inbound 774, contato `5548999490859`: houve produto real apresentado, mas a validação factual marcou falha; permanece um caso para revalidação após o novo deploy;
- contatos `5521981463519` e `5548999490859`: a qualificação de nome/orçamento continuou, sem erro de transporte, mas os rótulos de aprendizagem `greeting_steal` em saudações legítimas são ruído analítico e não evidência de falha enviada ao cliente.

## Alterações implementadas

- Normalização determinística da disjunção `open heart ou skeleton` como grupo OR.
- Movimento automático e cristal de safira passam a ser requisitos verificáveis no filtro local.
- Geração de duas consultas Tray independentes, uma por alternativa de estilo.
- Remoção de marca, modelo e shortlist herdados quando a mensagem atual inicia uma nova descoberta.
- Tradução de `current_price_range` para o parâmetro documentado `price_range` no TrayAdaptor.
- Fallback local do TrayAdaptor mantém o teto caso a loja rejeite a sintaxe remota.
- O cliente deixou de repetir consultas sem orçamento após erro 5xx.
- Respostas determinísticas de ausência, qualificação, orçamento sem resultado e indisponibilidade deixam de consumir/vetar uma segunda avaliação LLM.
- A resposta de ausência agora cita os critérios e oferece flexibilização controlada.
- Correção do `urlparse` no keepalive.
- Migração `027_ai_tray_sync_cursors.sql` criada e aplicada no Supabase.

## Comparação com projetos abertos

A correção segue padrões usados por frameworks maduros: estado explícito e transições determinísticas para evitar vazamento entre turnos, ferramentas com contratos estruturados e validação fora do texto gerado. Referências: [LangGraph](https://github.com/langchain-ai/langgraph) e [OpenAI Agents SDK for Python](https://github.com/openai/openai-agents-python). O agente existente já possui esses blocos; a mudança reforça os limites entre interpretação generativa, consulta determinística e validação factual, sem exigir uma troca de framework.

## Backtest

O caso regressivo verifica quatro candidatos sintéticos:

1. automático + safira + open heart por R$ 3.499,90: aceito;
2. automático + safira + skeleton por R$ 5.899,90: rejeitado pelo teto;
3. quartz + safira + open heart por R$ 2.500: rejeitado pelo movimento;
4. automático + safira + diver por R$ 3.000: rejeitado pelo estilo.

Também há testes para marca/modelo/shortlist obsoletos, tradução e fallback do filtro de preço, e preservação das respostas determinísticas pelo double-check.

## Critério de validação pós-deploy

Repetir a mensagem original em uma conversa que tenha histórico de outra marca. O trace deve mostrar:

- modo `recommendation`;
- ausência de `brand_lock`, `model_lock` e `sku_lock` sem marca/modelo atuais;
- probes separados para `open heart` e `skeleton`;
- teto de R$ 3.500 preservado em todas as tentativas;
- nenhum produto acima do teto em `commercial_data.products`;
- resposta transparente de ausência se o catálogo continuar sem correspondência;
- redução relevante frente às 19 chamadas Tray e aos 33 segundos do trace original.

