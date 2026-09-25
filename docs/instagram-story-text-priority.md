# Texto do cliente em respostas a Stories

O anexo do Story não implica uma pergunta sobre produto. Mensagens com sinais
de elogio, atendimento ou pós-venda seguem para a interpretação normal, inclusive
quando misturam agradecimento e pergunta comercial. A rota de imagem genérica
também não deve interceptar essas mensagens. A classificação e a redação do
atendimento continuam no fluxo existente; não foi criada uma resposta fixa de
elogio nem alterado o contrato de consulta de pedidos.

Mensagens comerciais como “quero um” continuam na resolução visual do Story.
Descrições livres da visão não são exibidas diretamente. Cores e posições são
normalizadas para português. Só se oferece escolha entre duas descrições quando
a análise informa dois relógios e as descrições são distintas. Em cenas com mais
relógios ou descrições ambíguas, solicita-se a posição ou um print marcado, sem
afirmar uma contagem com base no número de regiões retornadas.

Testes em `tests/stories/test_story_text_priority.py` verificam o roteamento dos
três incidentes, intenção mista, preservação da identificação comercial e ausência
de descrições internas em inglês. Esses testes não validam a redação de uma
resposta real do modelo nem uma consulta real de pedido.
