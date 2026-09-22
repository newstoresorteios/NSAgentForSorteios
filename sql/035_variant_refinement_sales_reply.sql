-- Operator-managed response after a customer chooses a product characteristic.
INSERT INTO public.agent_configuration_catalog(key, definition)
VALUES ('message.variant_refinement_sales_reply', $definition${"key": "message.variant_refinement_sales_reply", "target": "message", "type": "textarea", "label": "Resposta após escolha de característica", "group": "Continuidade e fechamento", "description": "Resposta comercial quando o cliente escolhe uma característica do produto apresentado. Preserve todas as variáveis entre chaves.", "maxLength": 4000, "variables": ["name", "characteristic", "price", "pix_price", "lead_time_days", "url"], "default": "{name}, na versão {characteristic}:\nA prazo: {price}\nÀ vista no Pix: {pix_price}\nPrazo informado pelo catálogo: {lead_time_days} dias úteis\nLink oficial: {url}\n\nVocê prefere finalizar pelo link oficial ou que eu abra seu pedido por aqui?"}$definition$::jsonb)
ON CONFLICT (key) DO UPDATE SET definition=EXCLUDED.definition, updated_at=now();

DO $$
DECLARE
  agent public.workspace_agents%ROWTYPE;
  next_configuration jsonb;
BEGIN
  FOR agent IN
    SELECT * FROM public.workspace_agents
    WHERE agent_type='nsagent' AND status='active' AND config_version > 0
  LOOP
    next_configuration := agent.configuration || jsonb_build_object(
      'schemaVersion', 2,
      'runtime', coalesce(agent.configuration->'runtime', '{}'::jsonb) || jsonb_build_object(
        'schemaVersion', 2,
        'values', coalesce(agent.configuration->'runtime'->'values', '{}'::jsonb) ||
          jsonb_build_object(
            'message.variant_refinement_sales_reply',
            $value$"{name}, na versão {characteristic}:\nA prazo: {price}\nÀ vista no Pix: {pix_price}\nPrazo informado pelo catálogo: {lead_time_days} dias úteis\nLink oficial: {url}\n\nVocê prefere finalizar pelo link oficial ou que eu abra seu pedido por aqui?"$value$::jsonb
          )
      )
    );
    PERFORM public.publish_workspace_agent_config(
      agent.workspace_id, next_configuration,
      'migration:variant-refinement-sales-reply', agent.config_version
    );
  END LOOP;
END $$;
