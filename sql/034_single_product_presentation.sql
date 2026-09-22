-- Present one best catalog match by default. Operators can change this later.
UPDATE public.agent_configuration_catalog
SET definition = jsonb_set(definition, '{default}', '1'::jsonb, true),
    updated_at = now()
WHERE key = 'catalogShortlistSize';

DO $$
DECLARE
  agent public.workspace_agents%ROWTYPE;
  next_configuration jsonb;
BEGIN
  FOR agent IN
    SELECT *
    FROM public.workspace_agents
    WHERE agent_type = 'nsagent'
      AND status = 'active'
      AND config_version > 0
  LOOP
    next_configuration := agent.configuration || jsonb_build_object(
      'schemaVersion', 2,
      'runtime',
        coalesce(agent.configuration->'runtime', '{}'::jsonb)
        || jsonb_build_object(
          'schemaVersion', 2,
          'values',
            coalesce(agent.configuration->'runtime'->'values', '{}'::jsonb)
            || jsonb_build_object('catalogShortlistSize', 1)
        )
    );
    PERFORM public.publish_workspace_agent_config(
      agent.workspace_id,
      next_configuration,
      'migration:single-product-presentation',
      agent.config_version
    );
  END LOOP;
END $$;
