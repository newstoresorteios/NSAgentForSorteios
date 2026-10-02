# Implementação e comprovação do plano de qualidade

Esta rodada implementa correções dos achados F01–F16, com testes de regressão, revisão dos diálogos e verificações de implantação. A autorização para usar a IA desta sessão foi preservada: nenhuma chamada paga de avaliação foi feita e nenhuma mensagem de teste foi enviada a clientes.

## O que mudou

| Achado | Correção | Comprovação e limite |
| --- | --- | --- |
| F01 — qualificação repetitiva | Interpretação precede pronta entrega; preferências conhecidas continuam nos atalhos; pedido explícito de lista não exige repetir roteiro. | Replays encadeados e regressões de descoberta. Interpretação remota não medida. |
| F02 — contexto apagado | Atualizações parciais preservam preferências e registram origem; remoção explícita e novo objetivo têm tratamento próprio. | Testes de evolução de estado, negativos, retomada e separação de sessões. |
| F03 — CPF tratado como pedido | Documentos são tipados; número curto em conversa sobre pedido/CPF recebe esclarecimento específico, sem virar orçamento. | CPF sintético válido e consulta de pedido com autorização preservada. |
| F04 — aprovação sem resolução | Revisores distinguem aprovado, reprovado e não avaliado; verificam contexto e repetição. Uma revisão útil pode acontecer mesmo sem orçamento para reparo completo. | Testes de orçamento e falhas do juiz. Ausência de modelo não é aprovação. |
| F05 — categoria pronta entrega | Navegação ampla separada dos filtros de produto; preferência anterior acompanha a consulta seguinte. | Contrato do adaptador e regressões da categoria. Anúncio não prova chegada ao cliente. |
| F06 — prazo ignorado | Ocasião/data desejada persistem; disponibilidade, postagem e chegada são distintas; data vencida usa relógio do turno. | Replays de casamento, disponibilidade e atraso; sem promessa de prazo sem cotação. |
| F07 — reparação estreita | Feedback semântico aciona reparo; frustração não vira cidade; fallback preserva pergunta já interpretada. | Casos “Já falei”, “Está difícil entender?”, “PQP” e negação de preferências. |
| F08 — instruções concorrentes | Política canônica; ativação valida conflito, duplicata e fragmentos inválidos; prompt registra somente extensões admitidas. | Testes compartilhados de runtime e aprovação pelo backend. |
| F09 — exemplos perdidos | Incidentes por turno, imutáveis; aprovação manual e automática preservam fontes; exportação de teste exige critérios revisados. | Idempotência, 141 revisões, concorrência e recuperação parcial. Fontes históricas já descartadas não foram inventadas. |
| F10 — avaliação sem versão | Manifesto com código, persona, configuração, modelo, adaptador e catálogo; orçamento igual ao runtime; gate explícito por caso. | 61 entradas revisadas. As 15 execuções antigas presas foram encerradas como inconclusivas, sem repetição paga. |
| F11 — falha de entrega | Alertas operacionais de falha permanente; prova de conta/permissão Meta separada da qualidade da resposta. | GETs da credencial local válidos; recibos reais históricos analisados. Nenhum envio de teste. |
| F12 — conhecimento literal | Corda manual e movimento automático são compatíveis; resposta técnica depende da pergunta, não do título do produto. | Regressões Seiko e pergunta de disponibilidade. |
| F13 — escopo da memória | Leitura, gravação, consolidação e cache por workspace; vínculo prompt/resposta; legado ambíguo isolado. O painel usa o workspace autenticado e o cache distingue loja, usuário e conversa. | SQL isolado: 17 critérios. PREPARE e ENFORCE aplicados, após conferir os escritores: 1.073 registros vinculados e 509 isolados no total. Seis triggers ativos; acesso do servidor preservado. |
| F14 — imagem/vídeo | Worker v6; origem da mídia/candidatos preservada; ambiguidade não é identidade exata. | Seis mídias originais, 21 frames e duas imagens inspecionados; sete testes específicos. Visão anotada, sem modelo remoto. |
| F15 — humano/retomada | Pedido explícito autoriza transferência; confirmação não volta a pedir consentimento; resumo factual no painel; oferta repetida conserva contexto. | Testes de consentimento, idempotência, resumo backend/frontend e pausa do bot. |
| F16 — banco legado | Migração isolada para revogar acesso público indevido e preservar acesso do servidor. | SQL ensaiado; aplicação no NS-db depende da autorização específica pendente. Não confundir com as duas migrações de memória autorizadas. |

## Prova de conversação

[Revisão por entrada](offline-october-review.md): 61 entradas de 23 conversas, sendo 51 executadas no pipeline com interpretação anotada por esta sessão e comércio sintético. A revisão identificou 48 atendidas nesse escopo e 13 inconclusivas. Não representa taxa de sucesso em produção.

As respostas, critérios e hashes completos estão no artefato local `.proof-results/october-incidents/session-review.json`. Os originais privados e rastros detalhados ficam fora do Git. Os testes visuais usam os originais, mas fornecem a interpretação visual anotada e marcam o catálogo indisponível; não medem acurácia remota nem confirmação de SKU.

## Implantação e migrações

As evidências de cada revisão implantada, resultados finais das suítes e estado das migrações são consolidados em [release-verification.json](release-verification.json). Push e HTTP 200 não são tratados como prova de qualidade conversacional.

PREPARE e ENFORCE foram aplicados após autorização explícita, na ordem validada. Antes do ENFORCE, foram confirmadas as versões do backend e agente, as filas sem reservas ativas, a ausência de chamadas da implantação antiga na janela consultada e o tempo superior ao limite documentado das funções antigas. Os agendamentos agora exigem a alias de produção e a revisão esperada antes de executar o cron; o teste em produção fez somente GET de saúde. O ENFORCE reconciliou mais cinco registros escritos entre as fases. O [runbook de rollback](memory-enforcement-rollback-runbook.md) explica por que restaurar código sem escopo após ENFORCE não é seguro.

Resultados finais: agente **3.227 aprovados, 7 ignorados e 76,69% de cobertura**; proteção dos agendamentos **17 aprovados** em execução separada; backend **304**; frontend **29**, TypeScript e build aprovados; adaptador **270**. O ensaio SQL passou **17 critérios**. O [CI da revisão operacional](https://github.com/newstoresorteios/NSAgentForSorteios/actions/runs/36944598747) também passou.

Backend, frontend, agente e adaptador tiveram suas revisões confirmadas nos provedores. As leituras de saúde do agente, backend e adaptador retornaram HTTP 200 após o ENFORCE. Os rastros privados permanecem locais, com hashes no relatório.

## O que ainda não pode ser certificado

O gate de qualidade remota permanece inconclusivo: não houve benchmark pago de interpretação/geração nem replay completo com catálogo real e mídia. Duas entradas usam o runtime MAI, fora deste agente; outras dependem de estado anterior ou identificador que não constam do export. Corrigir os caminhos reproduzidos e publicar o código não transforma essas lacunas em sucesso comprovado.

A rota administrativa protegida retornou 401 com a credencial local existente; o health público informa um aviso de configuração. Essa parte do diagnóstico não foi certificada, e não foram exportados segredos de produção. A migração separada do banco legado NS-db permanece aguardando autorização específica após bloqueio da revisão automática de aprovação.
