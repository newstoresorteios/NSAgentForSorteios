# Execução das melhorias — 15/09/2026

Autorização: executar as etapas em sequência, sem novas confirmações. Políticas,
persona, mensagens e parâmetros operacionais devem ser persistidos, versionados
e editáveis pelos operadores no ChatBo. Segredos permanecem no ambiente seguro.

## Sequência e estado

### Atualização da integração local

As etapas abaixo registram o estado inicial. O estado atual é:

- **Banco:** sete migrações aplicadas: RLS/grants, persona atômica, catálogo de configuração, despacho durável, edição atômica/mensagens, índices/documentos/traces, overview/aprovação atômica do aprendizado.
- **Configuração:** 450 definições, incluindo 250 mensagens/instruções. Editor por categoria e validação de tipos, limites, variáveis e políticas comerciais.
- **Runtime local:** orçamento/fallback, cache por turno e por versão, pool limitado, filtros de catálogo antes do limite, recuperação de erros transitórios, qualificação progressiva, checkout no site, URLs íntegras e aprendizado por workspace.
- **Frontend local:** rotas sob demanda, cache limpo entre sessões, histórico protegido contra troca de conversa, paginação e detalhes das consultas. Build aprovado; bundle principal passou de 884 para 706 kB.
- **Validações:** 134 testes backend antes da última integração; 755 testes comerciais NSAgent após migração de mensagens. Nova suíte integral pendente após ajustes RAG.
- **Publicação:** código ainda não publicado; CLI Vercel sem autorização. Cron do despacho criado e desabilitado até validar o endpoint publicado. Bootstrap das configurações dos workspaces pendente do novo runtime.
- **Em fechamento:** documentos com validade/versão na UI, aprendizado com contadores completos, contrato de atributos Tray, regressões e navegador.

### Registro inicial (histórico)

1. **Concluído em produção:** conter acesso público ao banco e verificar papéis internos.
2. **Banco recuperado; código local validando:** publicação transacional da persona,
   vínculo real de workspace e isolamento de testes. Release do backend pendente.
3. **Em andamento:** orçamento/fallback corrigido e 34 testes aprovados;
   catálogo de políticas e mensagens no banco;
   coerência de permuta entre atendimento e aprendizado.
4. Pendente: despacho durável frequente de inbox/outbox e idade da fila.
5. Pendente: contexto único por turno, cache versionado e consultas redundantes.
6. Pendente: precedência de configuração, qualificação progressiva e checkout.
7. Pendente: filtros antes do limite, atributos e recuperação da busca aproximada.
8. Pendente: validação final, links íntegros e estado consistente.
9. Pendente: avaliações e aprendizado por workspace, rollout/rollback e documentos.
10. Pendente adicional: completar configuração avançada, mensagens, aprendizado,
    consulta de traces e experiência de conversas no ChatBo; contratos TrayAdaptor.
11. Pendente: verificações integradas, migrações, publicação e conferência operacional.

## Alterações preexistentes preservadas

- Backend: whatsapp_meta.py, conversa_repository.py, mensagem_repository.py,
  routes/conversas.py, conversas_service.py, tests/test_conversation_read.py.
- Frontend: ConversationsPage.tsx, conversations.service.ts; tmp-login.json
  contém material local e não será incluído em artefatos/commits.
- NSAgent: relatórios e evidências de auditorias anteriores não rastreados.

## Decisões confirmadas

- O frontend utiliza a API ChatBo e não possui cliente Supabase direto.
- O backend autentica com JWT próprio e autoriza recursos pelo workspace.
- Credencial Supabase do backend: service_role; conexão SQL: postgres.
  Valores de credenciais não foram impressos nem incorporados neste relatório.
- As duas policies públicas de clientes/pedidos permitem ALL para PUBLIC e
  devem ser removidas. As tabelas são internas ao backend.
- Os testes de PersonaService injetam repositório falso, mas mantêm o publisher
  real: isso explica uma rota concreta para a publicação de IDs de fixture.

## Validação e evidências

Cada etapa registrará aqui testes executados, migrações aplicadas e limitações.
Nenhuma melhoria será considerada concluída apenas por ter seu código escrito.

- Migrações aplicadas no NsAgent: restrict_internal_data_api e
  atomic_workspace_persona_publication (arquivos no backend/supabase/migrations).
- Segurança: 0 tabelas sem RLS, 0 grants de tabelas para anon/authenticated/PUBLIC.
  Data API (limit=0, sem leitura de conteúdo) negou anon com 42501 e manteve 200
  para service_role em persona, workspace_agents, cache OAuth e clientes.
- Advisors: sem erros de segurança; INFO de RLS sem policy é esperado para
  tabelas internas. Restou WARN de pg_trgm em public, tratado na etapa de busca.
- Publicação: Crono New Store v17 recuperado e ligado ao workspace real.
  Recompilação do perfil ChatBo v20 + 1 anexo = 24.335 caracteres, hash idêntico;
  RPC retornou idempotent=true, sem criar nova versão nem arquivar a válida.
- Teste transacional no banco: vínculo persona-1/workspace-a rejeitado antes da
  ativação; conjunto de personas ativas preservado.
- Fallback: testes novos cobrem Responses/canary, texto/estrutura, orçamento zero
  e consumido, e estorno associado à reserva posterior ao checkpoint.
- A proteção dos testes do backend revelou dependências reais de autenticação
  não simuladas nos testes Mercos; foram substituídas por dependências explícitas
  de teste. Assertions antigas do wizard aposentado foram alinhadas ao contrato.
