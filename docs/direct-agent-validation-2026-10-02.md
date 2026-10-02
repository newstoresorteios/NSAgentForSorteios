# Validação do agente direto — 2026-10-02

## Problemas reproduzidos

- Lista truncada: adaptador retornava dez produtos; sanitização no agente cortava
  novamente para cinco, sem total ou continuação.
- Continuidade: o pedido de foto às 04:58 chegou com novo conversation_id da Brevo,
  mas mesmos workspace, canal, sender_key e visitor_id. Isso criou sessão vazia.
- Fotos: o agente não tinha uma ferramenta para anexar a imagem oficial do produto.
- Prazo: orientação geral da persona podia soar como chegada garantida para evento.

## Correções verificadas

- Total, quantidade retornada, próxima posição e snapshot público de dez minutos.
  Sem corte silencioso no agente. Snapshot expirado retorna 409 e exige atualização.
- Recuperação de até 24h de histórico entregue do mesmo cliente Brevo/WhatsApp,
  mantendo todos os limites de identidade. Consulta read-only do caso real recuperou
  34 mensagens e a recomendação do Longines anterior à abertura da nova conversa.
- Referências de produtos e última página preservadas nos metadados entregues.
- Anexo de imagem permitido somente para produto consultado com URL HTTPS do CDN.
- Instruções distinguem prazo geral e cotação de entrega para o destino.

## Evidências

- TRAYadaptor: 275 testes aprovados; OpenAPI verificado com query, offset, limit e
  snapshot_id. Build Render do commit 8b55b93 confirmado live.
- NSAgent: 137 testes aprovados, incluindo ingress, contratos, autenticação,
  expiração, continuidade, isolamento e imagem desconhecida.
- Consulta real ao site trouxe mais de 90 produtos. Totais variaram entre novas
  consultas, confirmando a necessidade de um snapshot para continuar a mesma lista.
- Bateria real via `/api/test/direct`, com a persona publicada e gpt-5.4-mini:
  primeira página informou 98 opções; segunda trouxe outros dez com o mesmo total
  e snapshot; recomendação para casamento não prometeu chegada; foto correta do
  Longines anexada; após reconstrução simulada, foto e link foram recuperados sem
  nova apresentação; garantia acionou search_knowledge; recusa não acionou handoff.

O script `scripts/eval_direct_conversation.py` permite repetir a sequência. Não
foram criadas mensagens de clientes, inbox ou outbox. A preparação do anexo foi
validada no preview; não foi feito envio real de WhatsApp/Instagram nesta bateria.
Uma avaliação comportamental não garante todas as formulações futuras do modelo.

## Rodada direct-v5: catálogo inteiro e vídeo

- 151 testes no NSAgent: entrada Meta/Brevo, contratos, catálogo completo,
  snapshots inválidos, foto por ID, Story expirado/sem URL, miniatura,
  transcrição, decoder real e limites de duração.
- 33 testes do contrato de pronta entrega no TRAYadaptor, incluindo OpenAPI.
  Nenhuma alteração no adaptador nesta rodada.
- Preview isolado na Vercel com persona publicada e modelo de produção:
  `overview_candidate_count=97`; alternativas distinguiram marca de cor;
  foto por identificador anexada no turno seguinte.
- A primeira execução revelou URL longa reescrita pelo modelo; a ferramenta
  passou a aceitar identificadores estáveis. Nova execução completou a foto.
- Bateria de sete turnos passou: primeira página, continuação no mesmo snapshot,
  casamento (nova comparação completa e modelos além das páginas mostradas),
  foto, reconstrução, garantia via search_knowledge, recusa sem handoff.
- MP4 com imagem de teste Hamilton: identificação visual de caixa retangular.
  Fixture com movimento e voz sintética: oito quadros recebidos no modelo,
  código falado 742 recuperado e formato retangular identificado.
- Nenhum envio real ao Instagram/WhatsApp e nenhum registro em inbox/outbox.
  O preview valida preparação e resposta; entrega pelo provedor é coberta por
  testes de integração locais, não por mensagem a cliente nesta rodada.
