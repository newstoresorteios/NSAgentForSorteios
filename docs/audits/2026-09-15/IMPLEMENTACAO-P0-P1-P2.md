# Implementação dos planos P0, P1 e P2

Data: 15/09/2026. Referência: incidente da consulta de automático com safira até R$ 2.500.

## Situação

Código implementado, verificado e publicado em NSAgentForSorteios, Chatbo-backendAgent e Chatbo-frontendAgent. Após o pedido de push, a migração foi aplicada em produção e os três deploys foram confirmados. A avaliação com modelo real continua pendente por limitação externa de credenciais/faturamento.

## Publicação confirmada em 15/09/2026

| Projeto | Commit em main | Deploy |
|---|---|---|
| Backend | `bf2ba18b097cad2b3f0945f704c2f60b1cf1a80c` | Render `dep-daktf8e8bjmc73fjmbng`, live |
| NSAgent | `eaf5c54556164b42fa56d0046ad1c8a282c003a1` | Vercel `dpl_3z4fhtHr3zxA9xroLHPQq3QB4TAf`, READY, produção |
| Frontend | `3746e3c7054d3dc1ba074b5e92bd5c75269671a1` | Vercel `dpl_5s24ehj15uLQSRpUJ5u4rd4fqJhx`, READY, produção |

- Backend publicado antes da migração; agente e frontend enviados após a verificação do banco.
- Migração `agent_technical_quality_controls` aplicada pelo conector Supabase; versão registrada no banco: `20260915234453`. Arquivo local: `20260915220149_agent_technical_quality_controls.sql`.
- Catálogo passou de 464 para 481 entradas. As 18 definições da migração correspondem integralmente ao arquivo. Hash das configurações e versões dos três agentes de workspace permaneceu igual ao anterior.
- Saúde do backend: HTTP 200, `status=ok`. Saúde do agente: HTTP 200, `ok=true`, SHA `eaf5c5455616`, banco configurado. Um aviso de configuração preexistente continua informado pelo endpoint.
- Frontend em `https://www.chatbo.com.br/`: HTTP 200, HTML servido. O SHA do deploy foi confirmado pela API Vercel usando a CLI autorizada; o conector tem acesso apenas ao projeto NSAgent.
- Os três HEADs locais correspondem a `origin/main`, sem alterações pendentes em arquivos rastreados. Dumps, logs, credenciais e relatórios locais ficaram fora dos commits.
- Evidências locais do banco: `quality-catalog-before-push.json` e `quality-catalog-after-push.json`.
- Estas verificações confirmam publicação e disponibilidade. Não substituem a avaliação generativa com modelo real, ainda pendente conforme a seção abaixo.

## Alterações

| Plano | Comportamento implementado |
|---|---|
| P0 — encaminhamento | Exige mensagem de entrada persistida, workspace proprietário, canal e conversa canônica. Telefone não seleciona conversas. Identificação ambígua ou ausente impede a atualização. Preserva atendente e conversas encerradas; registra o estado anterior na mesma transação. |
| P1 — critérios técnicos | Mecanismo e cristal passam do entendimento para filtros, evidências, resposta e memória. Cada produto recebe situação confirmada, incompatível ou desconhecida. Dados desconhecidos podem motivar consulta da ficha; não confirmam uma recomendação. |
| P1 — orçamento e revisão | Considera o limite complexo publicado, reserva capacidade antes de ranking opcional e exige saldo para regeneração + nova revisão. Indisponibilidade operacional não recebe nota zero nem provoca regeneração automática. Tentativas seguem o banco. |
| P1 — resposta e memória finais | Valida novamente depois da composição e do enriquecimento final. Memória e IDs permitidos contêm somente produtos identificados no texto entregue. Encaminhamento limpa uma lista não entregue. Atualiza o registro final usado pelo painel. |
| P1 — controles dos operadores | Vocabulário, expressões, mensagens de contingência, prompt de revisão, prompt de ordenação e limites da consulta têm definições no catálogo do banco. Formulários permitem editar os valores; backend valida formato e compatibilidade dos limites. |
| P1 — diagnóstico | Painel mostra modo publicado/aplicado, justificativa, indisponibilidade, tentativas, chamadas usadas, critérios, evidências por produto e produtos apresentados no texto final. |
| P2 — consultas e avaliação | Confirma fichas em lotes com concorrência e quantidade configuráveis; interrompe após confirmação suficiente ou falha transitória. Evita ranking/regeneração sem saldo. Inclui teste reproduzível com persona publicada, modelo real, catálogo isolado e interpretação da mensagem seguinte. |

Os textos e sinônimos usados pelos novos caminhos são lidos do banco em execução. O arquivo `sql/seeds/operator_catalog.json` fornece dados de instalação e a base dos testes; não é fallback de configuração em produção.

### Novas definições e migração

Migração no backend: `supabase/migrations/20260915220149_agent_technical_quality_controls.sql`.

- 17 definições novas: oito políticas e nove mensagens/instruções; catálogo passa de 464 para 481 definições.
- Atualiza também o padrão de `agent_max_llm_calls_per_turn` para 3, compatível com interpretação, composição e revisão. Isso totaliza 18 registros na migração.
- Valores já publicados em cada workspace são preservados. Edições posteriores continuam no fluxo existente de versões e histórico de configuração.
- Políticas novas: `catalogTechnicalFeatures`, `catalogFeatureRelaxationPhrases`, `catalogFeatureNegationPhrases`, `catalogTechnicalDetailLimit`, `catalogTechnicalSearchLimit`, `catalogTechnicalConcurrency`, `critiqueUnavailableAction`, `catalogReserveReviewCall`.
- A conduta padrão quando o revisor falha usa uma resposta reconstruída com fatos revalidados, quando o contexto permite. Caso contrário, usa a mensagem de encaminhamento cadastrada.

## Revisão das 20 conversas

Consulta somente de leitura confirmou os 20 IDs: 19 no workspace do incidente e um em outro workspace. No momento desta revisão, os 20 estavam em espera, com bot desativado, sem atendente atribuído e sem fusão registrada.

Não há registros de auditoria com a imagem imediatamente anterior dessas 20 conversas. Portanto, o estado anterior não permite um reparo automático verificável. Os registros foram preservados. A nova auditoria de encaminhamento evita essa ausência de evidência em ocorrências futuras.

Evidência local: `quality-affected-conversations-review.json`. Os dumps de atendimento devem permanecer locais; não são arquivos de implementação.

## Verificações

- NSAgent: **2.050 testes passaram**, seis pulados; três avisos de adaptação de data do SQLite nos testes.
- Backend: **163 testes passaram** na pasta `tests`; avisos de depreciação existentes. A coleta indiscriminada na raiz encontrou um log `.txt` anterior com codificação incompatível; a execução da pasta de testes passou.
- Frontend: TypeScript + Vite e lint dos arquivos alterados passaram. Permanece o aviso existente de tamanho de bundle.
- As 481 definições padrão passaram pelo validador do backend.
- PostgreSQL real: SQL de encaminhamento exercitado em tabelas temporárias com os tipos de produção; isolamento, auditoria e idempotência confirmados. Migração executada duas vezes em catálogo temporário: 18 registros, sem duplicação. Transação desfeita; nenhuma escrita em tabela ou sequência pública.
- Regressões incluem quartzo/mineral, solar, ficha desconhecida, sinônimo alterado pelo operador, mudança/relaxamento de critério, limite de consultas, falta de orçamento do revisor, falha operacional, estado após encaminhamento e alteração da resposta no último enriquecimento.

Resultados locais: `pytest-quality-full.txt`, `quality-postgres-verification.json`; backend `pytest-quality-controls.txt`.

## Avaliação com modelo real: pendência externa

A persona 17 e a configuração publicada v1 foram carregadas por conexões somente de leitura. A autenticação da CLI Vercel foi autorizada, mas `OPENAI_API_KEY` está marcada como sensível e não pode ser extraída por `env pull`.

A alternativa oficial de autenticação temporária via AI Gateway foi tentada. O serviço respondeu **HTTP 403 / `customer_verification_required`**, exigindo cartão cadastrado. Não foi cadastrado cartão nem alterado faturamento. **Nenhuma chamada ao modelo foi concluída com sucesso; não há resultado de qualidade generativa aprovado nesta execução.**

Para concluir com uma chave local de teste, no diretório NSAgent:

```powershell
python scripts/evaluate_conversation_quality.py --env-file <arquivo-local-com-OPENAI_API_KEY> --db-env-file <arquivo-local-com-DATABASE_URL> --workspace <workspace-uuid> --output <resultado.json>
```

O avaliador usa produtos capturados e uma opção positiva explicitamente sintética, permite somente consultas às fixtures, executa interpretação/composição/revisão e verifica a interpretação da mensagem seguinte. Não envia WhatsApp nem cria carrinho ou pedido. Ele sobrepõe os novos padrões apenas em memória para permitir testes antes da migração. Medir latência real com Tray/Vercel e confirmar a entrega ao canal exige uma verificação posterior à implantação.

## Ordem de implantação

1. Implantar o backend com validação dos novos formatos e diagnósticos.
2. Aplicar a migração `20260915220149_agent_technical_quality_controls.sql` pelo fluxo de migrações do projeto.
3. Implantar o NSAgent com os novos consumidores das políticas. Ele depende das definições da migração.
4. Implantar o frontend com formulários e diagnósticos.
5. Concluir a avaliação real com credencial disponível e conferir uma conversa de teste após a implantação.

O backend deve ser atualizado antes de disponibilizar os novos formatos no catálogo, para que a publicação das configurações pelos operadores continue funcionando.

### Repetir a verificação do SQL sem alterar produção

```powershell
python scripts/verify_quality_postgres.py --db-env-file <arquivo-local> --migration ../Chatbo-backendAgent/supabase/migrations/20260915220149_agent_technical_quality_controls.sql --output <resultado.json>
```

Esta verificação sempre usa tabelas temporárias e desfaz a transação externa, inclusive em caso de falha.
