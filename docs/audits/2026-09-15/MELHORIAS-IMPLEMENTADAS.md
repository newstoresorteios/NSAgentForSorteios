# Melhorias implementadas — NSAgent, ChatBo e TRAYadaptor

## Resultado da implementação

O catálogo operacional passa a ser lido do PostgreSQL: **451 definições**, incluindo
**250 mensagens e instruções**. O arquivo de seed é uma migração reproduzível e
fixture de testes; o runtime não o utiliza como fonte de configuração.

O operador altera valores em **Configurações → Configuração avançada do agente**.
Os campos têm categoria, descrição, tipo, limites e, para mensagens, variáveis
obrigatórias. A publicação cria uma versão; restauração carrega um rascunho.
Uma edição concorrente retorna conflito, preservando o rascunho do operador.

Identidade, tom, saudação, critérios e exemplos continuam no cadastro da persona.
Políticas comerciais publicadas prevalecem sobre inferências do texto livre.
Segredos, identidade das integrações e validações de segurança permanecem fora
do editor comercial. Novas mensagens/configurações exigem registro e migração.

## Etapas executadas

1. **Proteção do banco:** RLS e grants restritos; testes não abrem o cliente real.
   A persona de fixture foi arquivada; a persona legítima Crono v17 foi recuperada,
   vinculada ao perfil ChatBo v20 e ao workspace real.
2. **Persona transacional:** publicação e edição ativa validam identidade e versão;
   perfil, histórico e runtime são atualizados na mesma transação. Hash inválido
   reverte integralmente a alteração. O compilador usa o idioma e identidade do
   cadastro, sem impor uma marca fixa.
3. **Configuração e orçamento:** mensagens, instruções e controles migrados para o
   catálogo. Limite zero é respeitado. Falta de orçamento não aciona outro modelo;
   estorno de tentativa não devolve chamadas já consumidas.
4. **Filas:** aceitação durável antes do ACK, despacho após aceitação, backoff de
   falhas e consumidor periódico autenticado por segredo no Vault. O despacho
   periódico só é habilitado depois da validação do código publicado.
5. **Consultas e contexto:** cache por turno e por versão da configuração; consultas
   repetidas reaproveitam resultados e escritas invalidam o cache. Pool pequeno
   por processo, com fechamento dos pools removidos do cache.
6. **Fluxo comercial:** qualificação progressiva; modo de checkout configurável;
   fluxo no site dispensa CPF no chat. Política de permuta coerente com avaliação
   humana, sem estimativas de preço inventadas.
7. **Catálogo:** disponibilidade antes do limite, filtro de mecanismo, fallback
   após erro transitório, trigramas com índices coerentes e métricas das consultas.
   TRAYadaptor preserva atributos explícitos e suas fontes. Mecanismo explícito
   prevalece sobre palavras ambíguas no nome do produto.
8. **Resposta final:** links longos são preservados integralmente; estado comercial
   é realinhado após a última composição da resposta.
9. **Conhecimento e aprendizado:** documentos recentes, consulta contextual para
   perguntas curtas, trechos relevantes com identificador e hash de versão,
   validade configurável e histórico de alterações no banco. Propostas pendentes
   não entram na memória ativa. Aprovação e substituição de instruções são atômicas
   e isoladas por workspace. Contadores de 24 horas são calculados sobre o conjunto
   completo, com uma chamada para carregar a visão geral.
10. **ChatBo:** carregamento das rotas sob demanda; proteção do histórico ao trocar
    de conversa; limpeza de cache entre sessões; links clicáveis; paginação de
    execuções; filtros, quantidade de resultados, tempos e versões visíveis nos
    detalhes. Histórico de instruções rejeitadas, substituídas e expiradas.

As melhorias de envio otimista, mídias e confirmação de leitura preexistentes
foram preservadas. Elas foram registradas em commits próprios durante esta execução.

## Evidências de validação

- NSAgent: **1.993 testes aprovados, 1 ignorado**; 232 aliases de ambiente documentados.
- ChatBo backend: **139 testes aprovados**; avisos existentes de depreciação de datas.
- TRAYadaptor: **237 testes aprovados**.
- Frontend: TypeScript/Vite aprovados; lint dos arquivos alterados com **0 erros**
  e um aviso de Fast Refresh no contexto de autenticação.
- Bundle principal: aproximadamente **706 kB**, antes 884 kB; a redução local é
  de cerca de 20%. O alerta de chunk acima de 500 kB ainda existe.
- Navegador local com API simulada: publicação v1→v2, restauração como rascunho,
  carregamento dos controles, validade de documento com PATCH, contadores 125/42,
  histórico com motivo e consulta de catálogo com filtros e duração.
- Banco: 0 tabelas públicas sem RLS; advisors de segurança apenas informativos
  de RLS sem policy em tabelas internas. Acesso permanece pelo backend autorizado.
- Conexão local → banco: SELECT simples de cerca de 1,2 s com conexão nova para
  mediana de 399 ms com conexão reaproveitada. Rollback foi verificado. Isso não
  equivale a uma medição da latência total do agente em produção.
- Consulta de teste confirmou uso do índice de trigramas por Bitmap Index Scan.
- Antes da publicação, nenhuma entrada pendente nas filas (172 processadas,
  57 saídas enviadas). Nenhuma conversa de cliente foi criada para os testes.

## Migrações

Dez migrações em `Chatbo-backendAgent/supabase/migrations`, de
`20260915052413_restrict_internal_data_api.sql` até
`20260915072305_enable_verified_queue_dispatch.sql`.
Elas já estão aplicadas no projeto Supabase NsAgent.

## Acompanhamento da publicação

Os quatro componentes foram publicados. O runtime de produção confirmou a persona
v17 e a configuração v1 com 451 campos. O despacho durável está ativo a cada 15
segundos, sem pendências nas filas durante a conferência. Commits, endpoints,
correção UUID encontrada em produção e limites da evidência estão registrados em
`EXECUCAO-MELHORIAS.md`.

## Limites da evidência e continuidade

- O teste de navegador usa dados simulados; o resultado real de conversão depende
  de acompanhamento dos atendimentos e de avaliações representativas por workspace.
- A nova recuperação reduz ruído e usa versões; não transforma atributos ausentes
  na Tray em informação confirmada. A operação deve completar essas informações
  na origem e acompanhar filtros sem resultados nos traces.
- Aprendizado sem workspace exige atribuição antes de promoção automática.
- O catálogo exige migrações para cadastrar novas chaves; operadores editam os
  valores e as mensagens existentes, sem alterar os contratos técnicos das ferramentas.

## Recomendações de operação

1. Use a persona para identidade, comportamento, exemplos e critérios comerciais.
2. Mantenha regras objetivas no editor avançado; evite repetir políticas conflitantes
   em documentos, exemplos e instruções aprendidas.
3. Antes de ativar uma instrução aprendida, confira evidências e motivo; uma regra
   que altera política comercial deve ser refletida na configuração correspondente.
4. Cadastre validade em documentos temporários e remova/substitua conteúdo obsoleto.
5. Use execuções com poucos resultados, falhas e maior tempo para priorizar ajustes;
   compare períodos equivalentes e a mesma versão de configuração.
