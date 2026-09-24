# Reinício de busca e qualificação

## Incidente confirmado

Em 23/09/2026, os traces inbox-331/332 interpretaram pedidos de reinício como
continuidade do produto anterior. No inbox-333 houve limpeza da busca, mas no
inbox-334 a reidratação de memória permanente reinseriu cor, estilo, material,
ocasião e orçamento. Vercel registrou a reidratação; Render confirmou consultas
ao contrato interno com os filtros antigos e HTTP 200.

## Correção

- Reconhecer as formas imperativas de reinício, com resposta configurável e sem
  consulta comercial ou inferência para um pedido simples de reinício.
- Limpar preferências técnicas, atributos, produto e alvo selecionado; preservar
  identidade, cidade, endereço de entrega, carrinho e pedidos existentes.
- Usar estado/histórico da busca atual para continuidade. Memória comercial de
  longo prazo exige retomada explícita; remover essas preferências do prompt
  também. Resumos cumulativos sem fronteira de busca não entram após um corte
  de histórico; continuam disponíveis o estado estruturado e o histórico limitado.
- Qualificação editável: finalidade pessoal/presente, gênero, estilo e movimento,
  além de orçamento, tamanho, ocasião, cor e pulseira. A OpenAI redige a pergunta;
  respostas curtas são vinculadas ao campo perguntado. Não inferir gênero pelo nome.
- Separar estilo de ocasião. Exigir evidência do estilo declarado e considerar
  gênero estruturado no filtro. Todas as consultas continuam no TRAYadaptor.
- O fluxo adaptativo pode ultrapassar três perguntas úteis. A contingência admite
  dez perguntas distintas, configuráveis, e respeita pedido direto/sem preferência.

## Publicação

Aplicar `20260924001000_discovery_qualification_and_restart.sql` junto com a nova
versão do aplicativo. A migração atualiza os padrões do catálogo de configuração;
preserva overrides explícitos do operador. Não ativar as novas facetas em uma
versão antiga que não as reconhece. Nenhuma rota HTTP comercial foi modificada.

## Validação

Regressões locais sem chamadas pagas cobrem reinício seguido de marca curta,
limpeza de requisitos antigos, preservação de CEP/pedido, preferências no prompt,
estilo/gênero/movimento, presente, sem preferência, continuidade e schemas.
Testes locais não certificam a redação de um modelo real nem o deploy em produção.
