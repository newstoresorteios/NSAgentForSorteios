# Correção do HTTP 500 no webhook WhatsApp

## Causa confirmada

Os logs do deploy `0aa3c37` mostram `psycopg.errors.IndeterminateDatatype`, SQLSTATE
`42P18`, na resolução do workspace antes da geração da resposta. O terceiro
parâmetro aparece em `%s IS NULL`; essa expressão não fornece um tipo ao PostgreSQL.
O parâmetro seguinte, comparado com `channel`, é independente do terceiro.

A falha foi reproduzida no PostgreSQL com `channel='whatsapp'` e com `channel=None`.
Os logs conferidos também mostram zero chamadas ao modelo nessas tentativas.

## Correção

O filtro agora usa `%s::text IS NULL`. O parâmetro passa a ter tipo explícito,
mantendo o filtro por canal e a rejeição de conversas com workspace ambíguo.
Referência: [inferência de tipos de parâmetros no PostgreSQL](https://www.postgresql.org/docs/17/sql-prepare.html).

## Verificação

- 11 testes de resolução do workspace aprovados.
- Cinco casos usam o protocolo real psycopg/PostgreSQL: canal WhatsApp, Instagram,
  vazio, ausente e resolução de conversa existente com conferência do canal.
- Os testes de banco executam apenas SELECT, em transação de leitura, com timeout.
- A suíte completa, incluindo esses testes PostgreSQL, passou com **2.004 testes
  aprovados e 1 ignorado**.
- Não houve envio manual nem repetição de mensagens de clientes para testar o ajuste.

Os testes PostgreSQL exigem `NSAGENT_TEST_DATABASE_URL` explicitamente. A suíte não
reaproveita automaticamente `DATABASE_URL` nem lê arquivos com credenciais.
Com a variável configurada em ambiente de teste, execute:

```powershell
python -m pytest tests/configuration/test_workspace.py -q
```

Sem essa variável, cinco testes de integração são ignorados; os demais verificam
ausência de identidade/conexão, conversa desconhecida, UUID e ambiguidade.

## Limite da validação

Os testes simulados usados anteriormente não executavam essa consulta no PostgreSQL
e não detectaram o problema de tipagem. A cobertura de integração foi adicionada
para que esse contrato seja exercitado em um banco real quando configurado.
A conferência da nova versão e dos logs de produção é feita após a publicação.
