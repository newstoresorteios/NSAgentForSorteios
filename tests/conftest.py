"""Offline database catalog fixture; production loads these records from Postgres."""
import json
from pathlib import Path
from app.configuration.runtime import bind_bundle

_definitions = json.loads((Path(__file__).resolve().parents[1] / "sql/seeds/operator_catalog.json").read_text(encoding="utf-8"))
# Existing checkout fixtures explicitly exercise the assisted capability. New
# policy tests bind the production default (site) and assert that CPF/tool writes
# are prevented. The production seed remains site.
bind_bundle({"fields": _definitions, "values": {**{f["key"]: f["default"] for f in _definitions}, "checkoutMode": "assisted"}}, None)
