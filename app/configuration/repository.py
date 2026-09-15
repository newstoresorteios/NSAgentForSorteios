from __future__ import annotations
from typing import Any
from collections import OrderedDict
from copy import deepcopy
from threading import Lock

_bundles: OrderedDict = OrderedDict()
_lock = Lock()


def load_workspace_bundle(workspace_id: str, *, bootstrap_settings: Any = None) -> dict[str, Any]:
    from app.db import get_conn, to_jsonb
    from app.configuration.runtime import is_operator_setting
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT config_version,
                (SELECT max(updated_at)::text FROM public.agent_configuration_catalog) AS catalog_revision
                FROM public.workspace_agents WHERE workspace_id=%s::uuid AND agent_type='nsagent' AND status='active'
                """, (workspace_id,))
            revision = cur.fetchone()
            if not revision:
                raise RuntimeError("workspace_configuration_missing")
            key = (workspace_id, revision["config_version"], revision["catalog_revision"])
            with _lock:
                cached = _bundles.get(key)
                if cached is not None:
                    _bundles.move_to_end(key)
                    return deepcopy(cached)
            cur.execute("SELECT public.get_workspace_agent_bundle(%s::uuid) AS bundle", (workspace_id,))
            bundle = (cur.fetchone() or {}).get("bundle")
            if not bundle:
                raise RuntimeError("workspace_configuration_missing")
            if int(bundle.get("version") or 0) == 0 and bootstrap_settings is not None:
                # Capture effective deployment controls once, preserving operator edits.
                values = dict(bundle.get("values") or {})
                for field in bundle.get("fields") or []:
                    attr = field.get("attribute")
                    if field.get("target") == "setting" and attr and is_operator_setting(attr) and hasattr(bootstrap_settings, attr):
                        values[field["key"]] = getattr(bootstrap_settings, attr)
                cur.execute(
                    "SELECT public.initialize_workspace_agent_configuration(%s::uuid,%s) AS initialized",
                    (workspace_id, to_jsonb(values)),
                )
                cur.execute("SELECT public.get_workspace_agent_bundle(%s::uuid) AS bundle", (workspace_id,))
                bundle = cur.fetchone()["bundle"]
            key = (workspace_id, bundle["version"], revision["catalog_revision"])
            with _lock:
                _bundles[key] = deepcopy(bundle)
                while len(_bundles) > 32:
                    _bundles.popitem(last=False)
    return bundle
