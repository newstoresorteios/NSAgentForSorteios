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

    assert before["manifest_version"] == 1
    assert before["code"] == "unavailable"
    assert before["configuration"] == 10
    assert before["configuration_hash"] != after["configuration_hash"]
    assert before["source_hash"] != after["source_hash"]
    assert before["model"] == "gpt-test" and before["judge_model"] == "judge-test"
    assert before["case"] == "case-fingerprint"
