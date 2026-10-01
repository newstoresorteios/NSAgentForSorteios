insert into public.agent_configuration_catalog (key, definition)
values
(
  'business.raffle_group_url',
  '{"key":"business.raffle_group_url","target":"policy","attribute":"business.raffle_group_url","default":"https://chat.whatsapp.com/GdosYmyW2Jj1mDXNDTFt6F","label":"Grupo oficial dos sorteios","description":"Link oficial público do grupo de sorteios no WhatsApp.","group":"Políticas comerciais","type":"text","maxLength":30000}'::jsonb
),
(
  'message.raffle_group_reply',
  '{"key":"message.raffle_group_reply","target":"message","attribute":"raffle_group_reply","default":"Você pode entrar no grupo oficial dos Sorteios por aqui: {RAFFLE_GROUP_URL}\n\nAs ações e informações também ficam em {SITE_URL}","label":"Resposta com o grupo oficial dos sorteios","description":"Enviada quando o cliente pergunta como entrar ou pede o link do grupo. Preserve as variáveis entre chaves.","group":"Mensagens de atendimento","type":"textarea","maxLength":6000}'::jsonb
)
on conflict (key) do update
set definition = excluded.definition, updated_at = now()
where public.agent_configuration_catalog.definition is distinct from excluded.definition;

do $raffle_group_activation$
declare
    agent public.workspace_agents%rowtype;
    values_before jsonb;
    patch_values constant jsonb := jsonb_build_object(
      'business.raffle_group_url', 'https://chat.whatsapp.com/GdosYmyW2Jj1mDXNDTFt6F',
      'message.raffle_group_reply', E'Você pode entrar no grupo oficial dos Sorteios por aqui: {RAFFLE_GROUP_URL}\n\nAs ações e informações também ficam em {SITE_URL}'
    );
begin
    for agent in
        select a.* from public.workspace_agents a
        where a.agent_type = 'nsagent' and a.status = 'active'
          and exists (
              select 1 from public.ai_agent_persona_versions p
              where p.workspace_id = a.workspace_id and p.status = 'active'
                and p.tenant_id = 'newstore' and p.persona_key = 'newstore_commercial'
          )
        for update of a
    loop
        values_before := coalesce(agent.configuration #> '{runtime,values}', '{}'::jsonb);
        if not values_before @> patch_values then
            perform public.publish_workspace_agent_config(
                agent.workspace_id,
                agent.configuration || jsonb_build_object(
                    'runtime', coalesce(agent.configuration->'runtime', '{}'::jsonb)
                    || jsonb_build_object('schemaVersion', 2, 'values', values_before || patch_values)
                ),
                'migration:add-raffle-group-link',
                agent.config_version
            );
        end if;
    end loop;
end
$raffle_group_activation$;
