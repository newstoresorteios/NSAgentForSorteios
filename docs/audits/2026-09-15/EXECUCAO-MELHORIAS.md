# Execução das melhorias — 15/09/2026

## Resultado

As etapas foram implementadas e publicadas nos quatro componentes. Dez migrações
foram aplicadas ao Supabase NsAgent. O consumidor durável foi habilitado após
conferir o código publicado, sua autenticação e o carregamento da persona.

O workspace da persona ativa está na **configuração v1**, com **451 definições**,
incluindo **250 mensagens e instruções**. Os valores operacionais do ambiente foram
capturados pelo runtime de produção; alterações posteriores são versionadas pelo
editor. Outros workspaces ainda sem persona ativa recebem o catálogo e inicializam
seus valores quando passarem a utilizar o agente.

## Publicações e conferência

| Componente | Commits de implementação | Evidência em produção |
| --- | --- | --- |
| NSAgent | `a53afd4`, correção `af98c95` | Vercel READY; health confirmou `af98c957ad09`; runtime carregou persona 17, configuração 1 e 451 campos |
| ChatBo backend | `7de9fef`, registro operacional `2f8d974` | Health e banco saudáveis; OpenAPI contém PATCH de validade de anexos |
| ChatBo frontend | `2a3ad47`, ajuste de feedback `cc207cf` | Publicação pelo Git; site serve novos módulos de configuração, aprendizado e execuções |
| TRAYadaptor | `ab295d4` | Health confirmou `ab295d4d6598`; CI do GitHub aprovado |

Endereços conferidos:

- NSAgent: https://ns-agent-for-sorteios.vercel.app/api/health
- ChatBo: https://www.chatbo.com.br
- Backend: https://chatbo-backendagent.onrender.com/health
- TRAYadaptor: https://trayadaptor.onrender.com/health

## Etapas concluídas

1. **Banco:** RLS e grants restritos nas tabelas internas; acesso público removido.
2. **Persona:** publicação e edição ativa atômicas, controle de versão e identidade;
   isolamento de testes; recuperação da persona legítima Crono v17 / perfil v20.
3. **Configuração:** catálogo no banco, editor avançado, tipos, limites, variáveis de
   mensagens, conflitos de publicação e histórico/restauração como rascunho.
4. **Orçamento:** limite zero respeitado; esgotamento encerra tentativas de modelos;
   reserva e estorno não devolvem chamadas anteriormente consumidas.
5. **Filas:** aceite durável antes do ACK, processamento imediato, retomada periódica,
   autenticação dedicada no Vault e intervalo de retentativa após falhas.
6. **Consultas:** cache por turno e versão, pool limitado, invalidação após escrita;
   índices, disponibilidade antes do limite e recuperação após erro transitório.
7. **Atendimento:** políticas do workspace prevalecem; qualificação progressiva;
   checkout configurável no site; permuta com avaliação humana; links preservados.
8. **Catálogo e estado:** atributos explícitos da Tray preservados com origem;
   mecanismo explícito prevalece sobre nome ambíguo; estado alinhado à resposta final.
9. **Conhecimento e aprendizado:** seleção contextual de trechos, hash e validade;
   histórico de documentos; aprovação atômica e isolamento por workspace; propostas
   pendentes não entram na memória ativa; contadores de 24 horas completos.
10. **ChatBo:** rotas sob demanda, cache limpo entre sessões, histórico protegido
    contra troca de conversa, links clicáveis, paginação de execuções e consultas
    com filtros, resultados, duração e versões; histórico do aprendizado.

## Filas em produção

- Cron `nsagent-durable-queue-dispatch`: ativo, a cada **15 segundos**.
- Controle privado `queue_dispatch_settings.enabled`: **true**.
- Sem autorização: **HTTP 401**. Com segredo dedicado: **HTTP 200**.
- O log do processamento confirmou persona ativa e configuração carregadas do banco.
- Execuções às 07:23:30, 07:23:45 e 07:24:00 UTC terminaram com `succeeded`.
- Na conferência: 172 entradas processadas e 57 saídas enviadas; nenhuma pendência.
- Com filas vazias, o cron não solicita processamento ao Vercel.
- Nenhuma conversa de cliente foi criada nem mensagem de teste enviada.

## Validação

- NSAgent: **1.993 testes aprovados, 1 ignorado**; suíte final após a correção UUID.
- Backend: **139 testes aprovados**.
- TRAYadaptor: **237 testes aprovados**, incluindo preservação de especificações.
- Frontend: TypeScript/Vite aprovados; lint dos arquivos alterados sem erros.
- Navegador local com API simulada: configuração, publicação, histórico, validade,
  aprendizado e consultas; sem erros JavaScript observados.
- Build principal caiu de aproximadamente **884 para 706 kB** (cerca de 20%).
- Pool: mediana local de SELECT simples aproximadamente 399 ms nas conexões
  reaproveitadas; não representa a latência total do atendimento em produção.
- SQL: uso do índice de trigramas confirmado; rollback transacional confirmado.
- Segurança Supabase: nenhum aviso ou erro no advisor; apenas informes de RLS sem
  policy nas tabelas internas, acessadas pelo backend autorizado.

## Correções identificadas na verificação final

O primeiro teste publicado encontrou um UUID nativo do psycopg onde o modelo
esperava string. A persona não carregou nessa tentativa. Foi adicionada a conversão
no modelo e uma regressão; a leitura do registro real e a nova publicação confirmaram
a correção. A ativação do cron ocorreu depois dessa conferência.

O feedback de upload foi ajustado: falha na extração aparece como falha de leitura,
e sucesso de armazenamento não afirma uma republicação não confirmada.

## Migrações

Arquivos em `Chatbo-backendAgent/supabase/migrations`:

1. `20260915052413_restrict_internal_data_api.sql`
2. `20260915052534_atomic_workspace_persona_publication.sql`
3. `20260915053433_operator_configuration_catalog.sql`
4. `20260915055026_durable_queue_dispatch.sql`
5. `20260915061227_atomic_persona_edits_and_operator_messages.sql`
6. `20260915062507_catalog_retrieval_and_trace_performance.sql`
7. `20260915063904_workspace_learning_overview_and_atomic_approval.sql`
8. `20260915064908_workspace_learning_uniqueness_and_document_audit.sql`
9. `20260915070404_operator_template_contract_metadata.sql`
10. `20260915072305_enable_verified_queue_dispatch.sql`

## Limites e acompanhamento

- O cadastro controla persona, regras comerciais, textos e controles registrados.
  Credenciais, identidades de integração, contratos técnicos e validações de segurança
  ficam fora do editor comercial. Novas chaves exigem migração; valores existentes
  podem ser alterados pelos operadores.
- A conferência inclui endpoints reais, banco e arquivos servidos. A navegação
  autenticada das novas telas foi validada com uma API local simulada.
- O ambiente conferido tem uma persona ativa. Escala de consumidores e agendamentos
  para várias personas simultâneas exige teste de carga e distribuição por workspace.
- Aprendizado legado sem workspace precisa ser atribuído antes de promoção automática.
- O bundle principal ainda gera o aviso do Vite para chunks acima de 500 kB.
- O health do NSAgent informa um diagnóstico de configuração. O endpoint público
  fornece apenas a contagem; o detalhe exige o diagnóstico administrativo autenticado.
- Conversão e latência total precisam ser medidas em atendimentos representativos
  após a publicação; não foram estimadas a partir dos testes locais.

## Trabalho preexistente preservado

As melhorias de envio, histórico incremental, mídias e confirmação de leitura
preexistentes foram preservadas. Backend e frontend receberam commits próprios
dessas alterações durante a execução. Credenciais locais e arquivos temporários
não foram incluídos nos commits.
