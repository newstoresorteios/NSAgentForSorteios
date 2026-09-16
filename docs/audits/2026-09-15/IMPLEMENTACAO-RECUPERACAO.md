# Implementa??o do plano de recupera??o de conversa

Data: 15/09/2026. Escopo: sete etapas da an?lise do contato terminado em 8149 e falha adicional identificada na valida??o do cat?logo real.

## Publica??o

- NSAgent: `d090df4459af21a70e2505544ba2031848cc27bc`, produ??o Vercel READY. Inclui a implementa??o `ea27c0e` e a corre??o de disponibilidade.
- Backend: `88eed4ebd76b8227f2c342954ca146f115423210`, produ??o Render live.
- Migra??o aplicada: `20260915164212_conversation_recovery_integrity.sql`.
- Health HTTP 200 do NSAgent, backend e TRAYadaptor. O health do NSAgent confirmou o SHA final.

## Ajustes conclu?dos

| Etapa | Resultado |
|---|---|
| 1. Fluxo e refer?ncia | Marca/modelo ou refer?ncia expl?cita t?m prioridade sobre refer?ncias vagas ao produto anterior. A regra de pre?o exige inten??o de pre?o. |
| 2. Recupera??o | Corre??es e reclama??es usam crit?rios conhecidos para refazer a consulta. Repeti??o encaminha para atendimento conforme limite publicado no banco. |
| 3. Resposta rejeitada | O council verifica a sa?da final, evita repetir qualifica??o j? conhecida e preserva a interpreta??o na recupera??o. |
| 4. Contexto | Preserva??o de prefer?ncias, normaliza??o idempotente de modelo e limpeza de carrinhos ?rf?os ou expirados, preservando pedidos existentes. |
| 5. Cat?logo | Refer?ncia exata sem substitui??o silenciosa por outra da marca; atualiza??o de marcas configur?vel, incluindo indispon?veis para reconhecer sua identidade; compara??o de refer?ncia sem diferenciar mai?sculas. |
| 6. Central e persist?ncia | Hist?rico por workspace, sess?o e canal; identidade externa est?vel; cria??o concorrente protegida por ?ndice ?nico; reconcilia??o auditada. |
| 7. Configura??o e m?tricas | Novos textos, prompt do verificador, crit?rios de recupera??o e validade v?m do banco. Dura??o total usa tempo real, sem somar etapas aninhadas; chamadas efetivamente executadas preservadas nos metadados. |

## Persona e opera??o

A persona Crono carregou a vers?o 17, a configura??o publicada vers?o 1 e 464 defini??es. Foram adicionados 13 controles sem sobrescrever valores publicados pelos operadores.

O grupo **Continuidade e recupera??o** re?ne frases de corre??o, limite de encaminhamento, validade do contexto, marcas e limites de atualiza??o, mensagens de reconhecimento/encaminhamento e instru??o do verificador. O frontend existente l? o cat?logo dinamicamente.

## Banco: resultado verificado

- 57 conversas duplicadas mantidas como registros vinculados ? conversa principal.
- 670 registros de auditoria preservando a reconcilia??o.
- Zero grupos duplicados ativos.
- Zero entradas e zero respostas sem workspace.
- Zero diverg?ncias entre workspace da entrada e da resposta na verifica??o da migra??o.
- Auditoria protegida por RLS. O advisor acrescentou somente um aviso informativo de tabela com RLS sem pol?ticas, compat?vel com acesso exclusivo pelo servi?o; os avisos anteriores permanecem. Refer?ncia: https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy.

## Cat?logo real e falha adicional

A consulta autenticada ? API da loja confirmou o produto 4871, refer?ncia FAG03002B0, Orient Open Heart Preto. A API retornou `available=0` e `available_in_store=0`, apesar de quantidade num?rica de estoque positiva. Esses indicadores n?o autorizam apresentar o produto como dispon?vel para venda.

Foram consultados e atualizados os 80 produtos Orient. O ?ndice tinha dois. Durante a confer?ncia de persist?ncia, foi encontrada uma convers?o incorreta: `bool("0")` resultava em verdadeiro. A corre??o foi aplicada ao ?ndice e aos snapshots; os 80 itens foram regravados e a refer?ncia exata foi confirmada no banco com ambos os indicadores falsos.

A execu??o do fluxo de consulta com configura??o real e os fatos capturados da API produziu:

> Encontrei esse modelo no cat?logo, mas ele est? indispon?vel no momento. Posso procurar outras vers?es dele ou modelos semelhantes.

Essa verifica??o executou a leitura exata do ?ndice e a revalida??o usando os dados capturados. A interpreta??o foi fornecida ao cen?rio; n?o foi uma nova conversa enviada ao cliente.

## Valida??o

- NSAgent: 2.023 testes aprovados, seis opcionais ignorados na execu??o padr?o.
- Integra??o PostgreSQL executada separadamente: 11 testes aprovados, incluindo os cinco testes de banco ignorados na su?te padr?o.
- Backend: 146 testes aprovados.
- Frontend: seis testes de configura??o aprovados e build TypeScript/Vite conclu?do.
- Regress?es espec?ficas: corre??o com modelo expl?cito, falso pedido de pre?o, reclama??o repetida, prefer?ncias preservadas, carrinho ?rf?o, resposta final rejeitada, sess?o/workspace/canal e disponibilidade em formato textual/n?mero/booleano.
- Nenhuma mensagem de teste enviada ao contato, nenhum carrinho ou pedido criado.

## Limites da verifica??o

O novo atendimento ainda precisa ser observado em uso real para medir interpreta??o pelo modelo, lat?ncia completa, aceite pelo provedor e apresenta??o na central. O navegador de valida??o n?o inicializou porque a unidade C: est? sem espa?o. Os testes foram executados com tempor?rios na unidade D:. A CLI Vercel n?o estava autenticada; a publica??o foi acompanhada pelo conector, e o cat?logo foi consultado com a credencial de servi?o j? cadastrada.

O endpoint p?blico de sa?de informa um aviso de configura??o sem detalhar sua causa. Isso n?o impediu a inicializa??o, mas o health isolado n?o substitui valida??o de todas as integra??es.

## Evid?ncias locais

- `pytest-conversation-recovery.txt`
- `catalog-recovery-live.json`
- `orient-refresh-normalized.json`
- `captured-catalog-regression.json`

Os artefatos privados de auditoria ficaram locais; os commits publicados cont?m c?digo, migra??o e testes.
