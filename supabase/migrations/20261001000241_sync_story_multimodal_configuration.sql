-- Synchronize Story controls with the deployed multimodal implementation.
-- Catalog defaults alone cannot replace values already published by a workspace.
INSERT INTO public.agent_configuration_catalog (key, definition)
SELECT item->>'key', item
FROM jsonb_array_elements($story_catalog$[
  {
    "key": "instagram_story_media_max_bytes",
    "attribute": "instagram_story_media_max_bytes",
    "target": "setting",
    "label": "Instagram story media max bytes",
    "description": "Parâmetro INSTAGRAM_STORY_MEDIA_MAX_BYTES. Aplicado na próxima conversa após publicação.",
    "group": "Atendimento",
    "type": "integer",
    "default": 16777216,
    "readOnly": false,
    "min": 1024,
    "max": 52428800
  },
  {
    "key": "instagram_story_video_frame_analysis_enabled",
    "attribute": "instagram_story_video_frame_analysis_enabled",
    "target": "setting",
    "label": "Instagram story video frame analysis enabled",
    "description": "Parâmetro INSTAGRAM_STORY_VIDEO_FRAME_ANALYSIS_ENABLED. Aplicado na próxima conversa após publicação.",
    "group": "Atendimento",
    "type": "boolean",
    "default": true,
    "readOnly": false
  },
  {
    "key": "instagram_story_video_max_frames",
    "attribute": "instagram_story_video_max_frames",
    "target": "setting",
    "label": "Instagram story video max frames",
    "description": "Parâmetro INSTAGRAM_STORY_VIDEO_MAX_FRAMES. Aplicado na próxima conversa após publicação.",
    "group": "Atendimento",
    "type": "integer",
    "default": 8,
    "readOnly": false,
    "min": 1,
    "max": 10
  },
  {
    "key": "instagram_story_video_audio_enabled",
    "attribute": "instagram_story_video_audio_enabled",
    "target": "setting",
    "label": "Instagram story video audio enabled",
    "description": "Parâmetro INSTAGRAM_STORY_VIDEO_AUDIO_ENABLED. Aplicado na próxima conversa após publicação.",
    "group": "Atendimento",
    "type": "boolean",
    "readOnly": false,
    "default": true
  },
  {
    "key": "instagram_story_video_max_seconds",
    "attribute": "instagram_story_video_max_seconds",
    "target": "setting",
    "label": "Instagram story video max seconds",
    "description": "Parâmetro INSTAGRAM_STORY_VIDEO_MAX_SECONDS. Aplicado na próxima conversa após publicação.",
    "group": "Atendimento",
    "type": "integer",
    "readOnly": false,
    "default": 120,
    "min": 1,
    "max": 180
  },
  {
    "key": "instagram_story_audio_timeout_seconds",
    "attribute": "instagram_story_audio_timeout_seconds",
    "target": "setting",
    "label": "Instagram story audio timeout seconds",
    "description": "Parâmetro INSTAGRAM_STORY_AUDIO_TIMEOUT_SECONDS. Aplicado na próxima conversa após publicação.",
    "group": "Atendimento",
    "type": "number",
    "readOnly": false,
    "default": 12,
    "min": 1,
    "max": 30
  }
]$story_catalog$::jsonb) AS item
ON CONFLICT (key) DO UPDATE
SET definition = EXCLUDED.definition, updated_at = now()
WHERE public.agent_configuration_catalog.definition IS DISTINCT FROM EXCLUDED.definition;

-- This activation belongs only to the New Store commercial agent. Resolve its
-- workspace from the existing active persona instead of embedding generated IDs.
-- Keep every unrelated setting and record a new version through the normal API.
DO $story_activation$
DECLARE
    agent public.workspace_agents%ROWTYPE;
    values_before jsonb;
    patch_values constant jsonb := $story_values${
  "instagram_story_media_max_bytes": 16777216,
  "instagram_story_video_frame_analysis_enabled": true,
  "instagram_story_video_max_frames": 8,
  "instagram_story_video_audio_enabled": true,
  "instagram_story_video_max_seconds": 120,
  "instagram_story_audio_timeout_seconds": 12
}$story_values$::jsonb;
BEGIN
    FOR agent IN
        SELECT a.* FROM public.workspace_agents a
        WHERE a.agent_type = 'nsagent' AND a.status = 'active'
          AND EXISTS (
              SELECT 1 FROM public.ai_agent_persona_versions p
              WHERE p.workspace_id = a.workspace_id AND p.status = 'active'
                AND p.tenant_id = 'newstore' AND p.persona_key = 'newstore_commercial'
          )
        FOR UPDATE OF a
    LOOP
        values_before := coalesce(agent.configuration #> '{runtime,values}', '{}'::jsonb);
        IF NOT values_before @> patch_values THEN
            PERFORM public.publish_workspace_agent_config(
                agent.workspace_id,
                agent.configuration || jsonb_build_object('runtime',
                    coalesce(agent.configuration->'runtime', '{}'::jsonb)
                    || jsonb_build_object('schemaVersion', 2, 'values', values_before || patch_values)),
                'migration:sync-story-multimodal-configuration',
                agent.config_version
            );
        END IF;
    END LOOP;
END
$story_activation$;
