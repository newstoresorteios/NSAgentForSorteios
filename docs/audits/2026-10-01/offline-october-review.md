# Revisão das 61 entradas auditadas em 01/10/2026

Foram executadas 51 entradas no pipeline real, encadeadas por conversa, sem API paga ou rede. A interpretação foi anotada por esta sessão; catálogo, preços, estoque e pedidos são fixtures sintéticas. Isso verifica a lógica posterior à interpretação e os fallbacks, sem medir a qualidade do modelo remoto.

Revisão manual: **48 atendidas dentro desse escopo offline e 13 inconclusivas**. São 23 conversas e 61 entradas no denominador. O gate de qualidade para produção continua **inconclusivo**. Nenhum resultado foi promovido automaticamente.

Os manifestos registram código, configuração efetiva, persona, catálogo sintético e revisão local do adaptador. O adaptador foi simulado; ter seu SHA no manifesto não comprova acesso ao serviço implantado.

A revisão integral com critérios, respostas e hashes está em `.proof-results/october-incidents/session-review.json`; as observações brutas e rastros estão em `.proof-results/october-incidents/observations.json`. Esta revisão fica inválida se o hash do arquivo de observações mudar.

## Evidência visual original separada

Os originais de 1127, 1130, 1143, 1166, 1169 e 1179 foram inspecionados nesta sessão. Os seis casos passaram pelo worker com decodificação/PIL reais e visão pré-anotada; junto do teste de integridade, foram **7 testes aprovados**. O catálogo foi explicitamente marcado indisponível: nenhuma identidade exata, preço ou disponibilidade foi inventada. Isso não converte os sete casos dependentes de mídia em sucesso do pipeline completo. O texto 1167 foi correlacionado à foto 1166.

No vídeo 1127, há relógios distintos no pulso ao longo dos frames; o texto do anúncio não resolve sozinho “esse no seu braço”. No vídeo de 1143/1169 há quatro relógios. A foto 1179 é anexo, sem resposta a Story no envelope: não deve herdar automaticamente a identidade do vídeo anterior.

As anotações, hashes e limites estão em `tests/fixtures/story_media/october_annotations.json`. Os originais privados ficam fora do Git. A prova está em `tests/stories/test_october_real_media_evidence.py`, usando `STORY_REAL_MEDIA_DIR=.proof-temp/story-media`. Não houve chamada de IA paga nem transcrição de áudio.

## Versão revisada e gates de evidência

O corpus foi regenerado com sanitização de telefones, documentos, e-mails e URLs. Seu conteúdo sanitizado permaneceu idêntico. O replay foi repetido no commit registrado localmente e manteve as 51 respostas anteriores; a alteração semântica no estado foi explicitar `human_attendance.confirmed=false` para ofertas sem aceite. Os demais horários operacionais foram renovados.

- SHA-256 das observações: `cb26dffef21cdfd697f1b222efe2b29c423a6d06cd8c27fdfea9535ed304d725`.
- Hash do código-fonte: `f9bedfd12614e8048f57b62435be68502533cb7572a0b67b85bba03285795484`.
- Revisão Git executada: `0d753006b8b010a60ca57d57113a6e5d5f1093f6`; o hash do código-fonte permaneceu igual ao da execução pré-commit.
- Hash do JSON canônico do corpus (`json.dumps` com chaves ordenadas): `18955040d44982d539486b2124c3aacf9eb0914af6a7615bacbce728f58d8fae`. O SHA-256 dos bytes do arquivo está separado em `release-verification.json`.
- Revisão local do adaptador: `3a0c296641862aacd8c73d9ce3765a8cef729776` (execução simulada).

`assess_release_evidence` recebeu as 61 entradas normalizadas, com critérios, hashes e método de execução. O gate offline retornou **approved=false, 13 não verificadas**. O gate de produção retornou **approved=false, 61 não verificadas**. Não há entradas faltantes, duplicadas ou falhas observadas nos critérios limitados já atendidos; a ausência de prova completa impede aprovação.

Resultados normalizados: `.proof-results/october-incidents/normalized-results.json`. Gates: `.proof-results/october-incidents/release-gates.json`. Manifesto: `.proof-results/october-incidents/release-evidence-manifest.json`.

## Limitações e próximos critérios de aceite

- O estado comercial anterior ao dia não foi exportado. O replay parte de estado vazio, aproveita o histórico textual disponível e encadeia o estado produzido pelo candidato.
- Anotações corretas de intenção verificam o comportamento posterior à interpretação, sem medir a precisão do modelo. A categoria ausente e o estilo social inferido de casamento foram corrigidos nas anotações, sem contar essas correções como falhas resolvidas na produção.
- O filtro de estoque e os dados de pagamento/prazo do simulador foram alinhados aos contratos das ferramentas. Produtos, preços e datas continuam sendo dados sintéticos de teste.
- Sete entradas dependentes de mídia permanecem inconclusivas no pipeline completo. Seis mídias originais tiveram inspeção e worker testados separadamente, com visão anotada e catálogo explicitamente indisponível.
- Duas entradas pertencem ao runtime MAI e uma exige validação do ingresso após tomada humana. O documento original da entrada 1184 foi ocultado no export e não pode ser reconstruído por este teste.
- Nenhuma entrada recebe entrega=true sem confirmação real de envio. As 48 entradas atendidas neste escopo não representam taxa de aprovação do modelo remoto ou da produção.

## Pendências priorizadas

- **P1** — Completar o replay do pipeline com as mídias originais e a correlação entre identidade e catálogo, principalmente a ambiguidade entre relógios no pulso no caso 1127.
- **P1** — Medir interpretação e qualidade de respostas do modelo remoto em conversas reservadas para validação. Esta sessão offline não aprova a qualidade semântica remota.
- **P1** — Confirmar dependências e confirmação de entrega na produção separadamente da qualidade da resposta. O runtime MAI e o ingresso após tomada humana exigem evidências próprias.
- **P2** — Fornecer, em replay protegido, o estado comercial exato anterior ao turno e o identificador original dos casos cujo export não preservou essas informações.

## Revisão por entrada

| Entrada | Resultado | Avaliação e limite |
| --- | --- | --- |
| 1127 | Inconclusiva | Pipeline visual não executado nesta rodada. Inspeção separada identifica dois relógios no pulso ao longo do vídeo; alvo continua ambíguo. |
| 1128 | Atendida no escopo offline | Encerra SAIR com intenção farewell e conserva possibilidade de retomada. |
| 1129 | Atendida no escopo offline | Mantém encerramento com classificação farewell, sem iniciar qualificação. |
| 1130 | Inconclusiva | Vídeo original em avaliação visual separada; pipeline mídia/catálogo não executado nesta rodada. |
| 1131 | Atendida no escopo offline | Saudação inicial da persona. |
| 1132 | Atendida no escopo offline | Saudação inicial da persona. |
| 1133 | Atendida no escopo offline | Consulta pedido simulado e conserva vínculo para perguntas seguintes. |
| 1134 | Atendida no escopo offline | Saudação inicial da persona. |
| 1135 | Atendida no escopo offline | Pede pedido ou CPF/e-mail com alternativas explícitas. |
| 1136 | Atendida no escopo offline | Oferece CPF/e-mail quando cliente não tem pedido. |
| 1137 | Atendida no escopo offline | Confirma uso do CPF para consulta, sem convertê-lo em dado de pagamento. |
| 1138 | Atendida no escopo offline | Reconhece previsão vencida, não cria data de chegada e oferece consulta humana. |
| 1139 | Atendida no escopo offline | Saudação inicial da persona. |
| 1140 | Atendida no escopo offline | Falha de geração preserva pergunta específica anotada sobre o assunto; não reinicia apresentação. |
| 1141 | Atendida no escopo offline | Mantém pedido e reconhece explicitamente que a data passou. |
| 1142 | Atendida no escopo offline | Saudação inicial da persona. |
| 1143 | Inconclusiva | Vídeo com múltiplos relógios em avaliação separada; sem identidade exata validada por este replay. |
| 1144 | Inconclusiva | Marca Traska e busca encadeada observadas, mas estado do Story 1143 não foi reproduzido; seleção visual inconclusiva. |
| 1145 | Atendida no escopo offline | Confirma se o número curto é pedido; solicita 11 dígitos se intenção era CPF. Não presume orçamento. |
| 1146 | Inconclusiva | Aço é retido na continuação Traska; falta estado visual de 1143 para julgar redução de candidatos. |
| 1147 | Atendida no escopo offline | Explica previsão vencida sem apresentar pagamento como ação necessária. |
| 1148 | Atendida no escopo offline | Consulta lista pública simulada e distingue anúncio de estoque/entrega confirmados. |
| 1149 | Atendida no escopo offline | Consulta ampla retorna lista pública e limitações de confirmação. |
| 1150 | Atendida no escopo offline | Guarda casamento e prazo relativo; contextualiza lista e não promete chegada no evento. |
| 1151 | Atendida no escopo offline | Confirma pagamento a partir do contrato sintético payment.has_payment, sem cobrar novamente. |
| 1152 | Atendida no escopo offline | Reconhece correção e executa busca read-only; resultado respeita disponibilidade explícita da fixture. |
| 1153 | Atendida no escopo offline | Após limite de reparação, oferece equipe sem transferir sem consentimento. |
| 1154 | Atendida no escopo offline | Retoma casamento/prazo e deixa oferta humana disponível sem repetir pergunta ou presumir aceite. |
| 1155 | Atendida no escopo offline | Responde prazo de disponibilidade, distingue postagem/chegada e mantém SKU; não transforma inspeção em compra. |
| 1156 | Atendida no escopo offline | Mantém uso próprio e demais preferências; consulta candidato em vez de reiniciar perguntas. |
| 1157 | Atendida no escopo offline | Retoma consulta de pronta entrega e ressalva de estoque/prazo. |
| 1158 | Atendida no escopo offline | Feedback de frustração chega à reparação e consulta; não retoma entrevista. |
| 1159 | Atendida no escopo offline | Distingue disponibilidade, postagem e chegada; duração não vira orçamento. |
| 1160 | Atendida no escopo offline | Reformula explicação por etapas após feedback; não repete pergunta de CEP. |
| 1161 | Inconclusiva | Workspace usa MAI/XNamai, fora do runtime NSAgent exercitado. |
| 1162 | Inconclusiva | Workspace usa MAI/XNamai; não foi forçado a passar pelo agente errado. |
| 1163 | Atendida no escopo offline | Guarda dress/social; fixture não confirma combinação e resposta explicita a limitação. |
| 1164 | Atendida no escopo offline | Preferência dress chega ao atalho; ele cede à busca com restrições e evita lista genérica incompatível. |
| 1165 | Atendida no escopo offline | SAIR reconhecido e rotulado farewell. |
| 1166 | Inconclusiva | Imagem original em avaliação separada; pipeline de identificação/canal não executado. |
| 1167 | Inconclusiva | Texto depende da imagem 1166 e do estado gerado por ela; estado visual não foi fabricado. |
| 1168 | Atendida no escopo offline | Saudação inicial da persona. |
| 1169 | Inconclusiva | Mesmo vídeo multirrelógio de 1143 em avaliação visual separada. |
| 1170 | Atendida no escopo offline | Disponibilidade informada com fonte sintética; nome Automático não vira pergunta extra de mecanismo. |
| 1171 | Atendida no escopo offline | Proposta comercial classificada fora do escopo, sem trade-in por trocarmos uma ideia. |
| 1172 | Atendida no escopo offline | Pede identificador de pedido ou alternativa. |
| 1173 | Atendida no escopo offline | CPF validado consulta titular e lista pedidos; não é persistido como número do pedido. |
| 1174 | Atendida no escopo offline | Documento isolado pede finalidade; não inicia consulta de pedido por inferência da anotação. |
| 1175 | Atendida no escopo offline | Confirma finalidade pendente, sem converter CPF em pagamento. |
| 1176 | Atendida no escopo offline | Consulta pedido e preserva contexto para atraso. |
| 1177 | Atendida no escopo offline | Reconhece atraso e previsão vencida com dados confirmados da fixture. |
| 1178 | Atendida no escopo offline | Respeita recusa de encaminhamento e informa limites do atendimento automático. |
| 1179 | Inconclusiva | Foto enviada como anexo, sem reply_to_story; vínculo ao vídeo anterior não prova identidade da foto. |
| 1180 | Atendida no escopo offline | Confirma contextualmente a pulseira mencionada no histórico; modelo semântico é pré-anotado. |
| 1181 | Atendida no escopo offline | Saudação inicial da persona. |
| 1182 | Atendida no escopo offline | Pede pedido ou identificador alternativo. |
| 1183 | Atendida no escopo offline | CPF consulta titular/pedidos antes de qualquer interpretação como pedido. |
| 1184 | Inconclusiva | Export substitui documento original por [document]; não permite revalidar identificação original. |
| 1185 | Atendida no escopo offline | Pedido explícito por humano produz encaminhamento sem pergunta redundante; entrega real não exercitada. |
| 1186 | Atendida no escopo offline | Sim mantém encaminhamento já solicitado, sem pedir autorização novamente. |
| 1187 | Inconclusiva | Tomada humana pertence ao ingresso/CRM; silêncio não foi simulado como resposta bem-sucedida. |
