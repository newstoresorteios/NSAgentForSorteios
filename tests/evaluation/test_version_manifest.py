from app.evaluation.version_manifest import build_version_manifest, source_tree_hash


def test_manifest_tracks_code_configuration_models_and_case(monkeypatch, tmp_path):
    app_dir = tmp_path / "app" / "runtime"
    app_dir.mkdir(parents=True)
    source = app_dir / "agent.py"
    source.write_text("answer = 1\n", encoding="utf-8")
    source_tree_hash.cache_clear()
    monkeypatch.delenv("VERCEL_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("VERCEL_URL", raising=False)
    bundle = {"version": 10, "values": {"conversationRepairAfter": 2}}

    before = build_version_manifest(
        persona_version="persona-v3", bundle=bundle, model="gpt-test",
        judge_model="judge-test", case_hash="case-fingerprint",
        mode="historical_context_replay", root=tmp_path,
    )
    source.write_text("answer = 2\n", encoding="utf-8")
    source_tree_hash.cache_clear()
    after = build_version_manifest(
        persona_version="persona-v3", bundle={"version": 11, "values": {"conversationRepairAfter": 3}},
        model="gpt-test", judge_model="judge-test", case_hash="case-fingerprint",
        mode="historical_context_replay", root=tmp_path,
    )

    assert before["manifest_version"] == 2
    assert before["code"] == "unavailable"
    assert before["configuration"] == 10
    assert before["configuration_hash"] != after["configuration_hash"]
    assert before["source_hash"] != after["source_hash"]
    assert before["model"] == "gpt-test" and before["judge_model"] == "judge-test"
    assert before["case"] == "case-fingerprint"
    assert not before['reproducible']
    assert {'persona_hash', 'catalog_hash', 'adapter_revision'} <= set(before['missing_evidence'])


def test_manifest_invalidates_changed_catalog_and_persona(tmp_path, monkeypatch):
    (tmp_path / 'app').mkdir()
    monkeypatch.setenv('GIT_COMMIT_SHA', 'revision')
    args = dict(persona_version='v1', bundle={'values': {}}, model='offline', case_hash='case',
                adapter_revision='adapter-sha', root=tmp_path)
    first = build_version_manifest(**args, persona_content={'prompt': 'one'}, catalog_snapshot={'price': 1})
    second = build_version_manifest(**args, persona_content={'prompt': 'two'}, catalog_snapshot={'price': 2})
    assert first['reproducible']
    assert first['persona_hash'] != second['persona_hash']
    assert first['catalog_hash'] != second['catalog_hash']


def test_extra_cannot_forge_version_identity():
    import pytest
    with pytest.raises(ValueError, match='reserved_field'):
        build_version_manifest(persona_version='v1', bundle={}, model='offline', extra={'code': 'forged'})
