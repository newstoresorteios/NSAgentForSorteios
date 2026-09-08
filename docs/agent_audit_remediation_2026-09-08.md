# Correções da auditoria adicional — 08/09/2026

As correções da auditoria de 07/09 foram implementadas e revisadas em conjunto com as alterações posteriores do projeto. A base da verificação final é `871516f`, acrescida das correções de fixtures e dos testes de serialização entregues nesta etapa. Parte da implementação já foi incorporada aos commits `c06f43f` e `871516f` durante o trabalho compartilhado; esses commits não foram criados nesta etapa de validação.

## Resultado verificado

- Suíte completa local, excluindo `online_eval`: **1.879 testes passaram; 1 desmarcado**, em 20,77 s. Inclui avaliações offline e regressões novas; não somar resultados de subconjuntos.
- Contrato do aprendizado na base acessível: consulta de metadata aprovada; tabelas `ai_learning_cursors` e `ai_learning_cases` presentes, ambas com RLS. Verificação em transação com `transaction_read_only=on`. A ausência observada em 07/09 não persiste nessa base.
- Nenhuma compra, cobrança, mensagem ou alteração de banco foi executada como teste. Não houve chamada paga de modelo nem implantação nesta etapa.

## Correções por achado

| Achado | Comportamento corrigido | Evidência principal |
|---|---|---|
| R1 | Conselho recompõe texto a partir de candidatos individualmente elegíveis antes de descartar a lista. Fato rejeitado, preço ausente/inválido, indisponibilidade e near match continuam bloqueados. Bloqueio não fabrica catálogo vazio. | `tests/sales/test_council_recomposition.py` |
| R2 | Worker serializa a mesma conversa; retomada com lock já adquirido evita deadlock. Claim da inbox respeita o predecessor ainda não terminado. | `tests/ingress/test_worker_conversation_serialization.py`, `test_delivery_regressions.py` |
| R3 | Envio imediato e retentativa disputam a mesma reserva atômica. Confirmações carregam o proprietário da reserva; envelope aceito é imutável. | `tests/ingress/test_delivery_regressions.py` |
| R4 | Resposta aceita é recuperada antes da geração; recibo da outbox impede novo envio mesmo sem registro em respostas. Retentativas preservam texto, metadata e provedor. | `tests/ingress/test_delivery_regressions.py`, `test_outbox_envelope.py` |
| R5 | Indicadores distinguem bloqueio do conselho, ausência de candidato dentro das restrições e falta de contexto. | `app/ops/integrity_kpis.py`; suíte de operações |
| R6 | Metadata vem do JSON já persistido. Falha de leitura, persistência, cursor ou avaliação impede sucesso silencioso e promoção. Cursor avança apenas pelo prefixo persistido. | `tests/learning/test_learning_failure_contract.py`; verificação real somente leitura |
| Catálogo C01 | Diver, tamanho de caixa e cronógrafo são filtros cumulativos. | `tests/catalog/test_live_recommendation_contract.py` |
| Catálogo C02 | Preço/disponibilidade são conferidos após consulta live. Candidatos invalidados não retornam via cache; reposição usa reservas com limite. | `tests/catalog/test_live_recommendation_contract.py` |
| Memória M1 | Recuperação de pedido antigo preserva preferências e orçamento atuais. | `tests/memory/test_audit_memory_regressions.py` |
| Memória M2 | Nova marca explicitamente declarada substitui a memória anterior de ausência de preferência. | `tests/memory/test_audit_memory_regressions.py` |
| Memória M3 | Pedido de esquecer não é descartado como duplicação de valor. | `tests/memory/test_audit_memory_regressions.py` |
| Memória M4 | Tipo de orçamento não libera fatos voláteis de estoque ou pagamento. | `tests/memory/test_audit_memory_regressions.py` |
| LLM M5 | Reranking opcional preserva a chamada disponível para composição final. | `tests/llm/test_rerank_response_budget.py` |
| Comércio C1 | Cursor de eventos Tray não ultrapassa evento cuja aplicação falhou; resposta inválida não aparenta consulta vazia bem-sucedida. | `tests/tray/test_tray_webhook_consumer.py` |
| Comércio C2 | Confirmação de PIX pelo pedido respeita identidade e validação financeira. | `tests/commerce/test_pix_integrity_regressions.py` |
| Comércio C3 | Identidade de pagamento inclui revisão/snapshot confirmado, evitando colisão entre compras diferentes de mesmo total. | `tests/commerce/test_pix_integrity_regressions.py` |
| Comércio C4 | Elegibilidade de retentativa é filtrada antes do limite; falhas permanentes não ocupam toda a página. | `tests/commerce/test_pix_integrity_regressions.py` |
| Comércio C5 | Pedido exige sessão comprovada; resposta parcial/inválida não autoriza criar outro pedido. | `tests/commerce/test_pix_integrity_regressions.py` |

## Ajustes encontrados durante a implementação

Abertura de marcas também invalida o binding anterior e remove restrição legada de marca. Orçamento informado sozinho preserva centavos; continuidade explícita preserva preferência de marca, enquanto uma nova busca evita herdar restrições antigas. Uma recomendação generativa precisa identificar um candidato por nome, referência ou URL; texto genérico volta à lista factual sem consumir outra chamada. O scanner não considera qualquer token iniciado por `tok-` uma fixture.

Três falhas surgiram na primeira suíte conjunta: dois mocks da fila ainda usavam contratos anteriores e um teste novo do scanner tinha argumentos incorretos. Foram corrigidas antes da execução final aprovada. Os testes da fila continuam verificando enqueue antes do envio, confirmação posterior e reaproveitamento do lock.

## Implantação e limites

`sql/026_learning_schema_repair.sql` é uma reparação aditiva e idempotente para instalações antigas sem as tabelas de aprendizado. Não exclui avaliações. Não foi aplicada nesta etapa; a base consultada já possui as tabelas. Validar o papel de serviço antes de aplicar em outro ambiente, pois RLS exige permissões adequadas.

A suíte usa isolamento offline e Python 3.13 local; CI configura Python 3.12. Não foi confirmado o SHA efetivamente implantado em Vercel/Render nem executado replay com o modelo real. Resultados locais não são uma medição da qualidade de produção.

A reserva da outbox fecha a corrida demonstrada entre envio imediato e retry. Ela não constitui garantia universal de entrega exatamente uma vez: um timeout após aceite externo, antes de persistir o recibo, continua sendo um estado ambíguo que depende da idempotência/consulta oferecida pelo provedor.

Artefatos locais de teste e validação: `docs/audits/2026-09-08/pytest-final.txt`, `pytest-final.xml` e `live-learning-contract.json`. Amostras de conversas da auditoria anterior não devem acompanhar uma publicação pública de código ou relatório.
