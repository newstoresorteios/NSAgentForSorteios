# Identificação visual e links do catálogo — 16/09/2026

## Evidências

Os anexos foram correlacionados com ai_inbound_messages/ai_agent_responses, logs da Vercel e requisições do TRAYadaptor no Render. Horários abaixo em America/Sao_Paulo.

| Entrada | Horário | Resultado observado | Divergência |
|---|---|---|---|
| 838 | 11:28 | Hipótese Hamilton Khaki Field Murph, sem referência confirmada | Busca não encontrou a variante correta |
| 839 | 11:30 | Mesma mídia, resposta Hamilton Khaki Navy H82345141 | Nova interpretação e busca por texto trocaram a família |
| 840 | 11:59 | Brew Retrograph, sem resultado confirmado | Consulta incluiu brown; página oficial usa marrom |
| 841 | 11:59 | Hamilton H70305143, 40 mm | Revisão generativa promoveu candidato textual a identidade visual |
| 842 | 12:01 | Link H70305143 | URL da API redirecionava para sem-resultados-na-busca |

Na entrada 841, os logs registram image_product_identify, dois judges e response_composition: 60.613 tokens e aproximadamente 46,5 segundos. A validação factual declarou valid=true, mas checked_claims=0: ter dados de um produto na API não comprovou que ele era o produto fotografado.

O Render confirmou consultas HTTP 200; não houve indisponibilidade do endpoint nessas requisições. O problema foi seleção/evidência. As consultas usaram `Retrograph brown Cronógrafo`, `brand=Brew` e `Khaki Field Automático`, além de buscas amplas. A base consultada não continha os produtos corretos 15998/16010 no índice comercial e tinha zero linhas no índice visual. Esses índices vazios/incompletos não podiam sustentar uma negativa sobre a existência no site.

Outro defeito: ensure_product_has_live_url marcava um endereço como morto, mas preservava a URL original; official_product_url a devolvia novamente. A checagem também aceitava páginas sem verificar o identificador do produto e propunha alterações de sufixo que poderiam trocar a variante.

## Correção geral

- Toda identificação de foto usa uma autoridade de evidência visual. Marca/modelo produzidos pela IA servem para descobrir candidatos; não confirmam referência, dimensão ou preço.
- As consultas normalizam cores, removem termos genéricos e leem páginas seguintes, com limites configurados no banco. Nenhum modelo específico foi cadastrado no algoritmo de resolução.
- A confirmação compara contorno e cor da foto com as imagens oficiais, exige margem sobre as variantes concorrentes e rejeita comparação incompleta.
- A página de destino precisa conter dados estruturados do mesmo product_id. A confirmação separa identidade e disponibilidade. Texto de modal oculto não define estoque.
- O revisor genérico não reescreve identificações visuais. O fechamento do turno reconstrói a resposta e a memória a partir da evidência, mesmo se uma etapa intermediária tentar trocar o produto.
- A validação final verifica links do catálogo efetivamente presentes na resposta. Links rejeitados são retirados também dos dados que poderiam ser reutilizados no próximo turno.
- O ingresso Brevo lê a configuração publicada antes de escolher execução direta ou fila. Antes, a configuração de agrupamento podia ser aplicada apenas depois dessa decisão.
- Mensagens, tradução de cores, limites de busca/comparação, domínios e remoção de asteriscos ficam no catálogo de configuração editável pelos operadores.

## Validação

Reprodução local com as mídias originais, a interpretação capturada e consulta atual ao site, sem chamada ao modelo:

| Caso | Produto encontrado | Distância de contorno | Erro médio de cor | Tempo medido |
|---|---|---|---|---|
| Brew | 15998 — Retrograph Espresso Chronograph Meca-Quartz Marrom 38 mm | 0 | 0,000374 | 6,36 s |
| Hamilton | 16010 — Khaki Field Murph Azul H70405740 38 mm | 0 | 0,000400 | 15,15 s |

O Hamilton foi encontrado na segunda página de `hamilton khaki field azul`; foram comparadas 23 imagens e a segunda variante teve distância 12. A página do Brew não publica referência comercial; o vínculo usa o product_id, sem inventar referência. O link incorreto do H70305143 passou a retornar URL nula e indicação de link rejeitado.

Os testes automatizados cobrem família/modelo genéricos, ausência de referência, variantes ambíguas, cores distintas com contorno igual, imagem concorrente indisponível, paginação, modal oculto, URL de outro produto, redirecionamento para página sem resultado, troca de SKU pelo revisor, memória entre fotos e o pipeline completo de resposta. Os testes antigos que permitiam confirmar identidade por texto foram substituídos por esses contratos.

Execução final: 2.211 testes passaram, 6 foram ignorados e houve 8 avisos de depreciação do adaptador datetime do SQLite nos testes existentes. A migração 030 foi aplicada e as 15 novas definições foram conferidas no bundle do workspace.

## Limites

Não existe garantia de identificação perfeita para qualquer foto. Recortes, ângulos diferentes, imagens iguais reutilizadas entre variantes, falhas de download ou limite de descoberta podem impedir confirmação. Nesses casos a resposta pede uma foto mais detalhada, sem apresentar outro relógio como se fosse o solicitado. A comparação atual favorece fotos iguais ou próximas às imagens oficiais; não substitui uma avaliação com um conjunto amplo de fotos de clientes.

As reproduções não enviaram mensagens ao contato, não executaram uma nova conversa com modelo pago e não ativaram regressão automática. Os tempos acima são medições locais da resolução após a interpretação, não latência garantida do WhatsApp.
