# Fotos nativas no WhatsApp

O texto transacional da Brevo é enviado integralmente, sem o corte de 1.024
caracteres. O limite de legenda não é aplicado às respostas de texto.

A API pública da Brevo documenta texto/template em `POST /v3/whatsapp/sendMessage`.
O contrato de `POST /v3/conversations/messages` também não documenta campos de
upload de imagens. `imageUrl` e `attachments` nessas requisições não são usados
como prova de envio da foto.

O transporte opcional `app/channels/whatsapp_media.py` envia o arquivo:

1. Confirma que o Phone Number ID da Meta corresponde ao `BREVO_SENDER_NUMBER`.
2. Baixa as fotos selecionadas do CDN permitido, com limite de 5 MB por arquivo.
3. Verifica JPEG/PNG e faz upload multipart dos bytes em `/{phone-number-id}/media`.
4. Envia `type=image` com o media ID retornado em `/{phone-number-id}/messages`.
5. Envia o texto completo pela rota transacional existente, separado da imagem.

Não há fallback para link de imagem. Uma falha de configuração, upload ou envio
retorna erro; resposta HTTP de sucesso sem identificador de mensagem não basta.
Uma confirmação da API significa aceite pelo provedor, não confirmação de leitura
ou entrega ao aparelho. Falhas parciais ou envios com resultado incerto exigem
investigação operacional, evitando repetir automaticamente as fotos já aceitas.

## Configuração

```env
META_WHATSAPP_MEDIA_ENABLED=false
META_WHATSAPP_ACCESS_TOKEN=
META_WHATSAPP_PHONE_NUMBER_ID=
META_WHATSAPP_GRAPH_VERSION=v23.0
```

Somente habilitar após configurar acesso da Cloud API ao mesmo número da Brevo.
O token do Instagram não é reutilizado automaticamente. Essas variáveis são
operacionais; a persona e o modelo não podem alterá-las.

Sem esse acesso, a implementação está preparada, mas fotos reais continuam
indisponíveis. Os testes locais usam transporte HTTP simulado, verificando bytes,
autenticação, identificação do remetente e falhas; não enviam mensagens a clientes.
Validar com um destinatário de teste explicitamente autorizado antes da ativação.

## Referências oficiais verificadas

- [Brevo: mensagens WhatsApp](https://developers.brevo.com/docs/whatsapp-messages)
- [Brevo: contrato Conversations](https://developers.brevo.com/openapi/conversations.json)
- [Meta: upload e envio de mídia](https://whatsappbusiness.com/blog/media-messages-via-app/)
- [Meta: imagem por media ID](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/messages/image/)
