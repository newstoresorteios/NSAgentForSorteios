import pytest

from app.stories.story_highlight_references import StoryHighlightReferenceRepository
from app.stories.story_highlight_references import StoryHighlightReference


def test_matches_unique_configured_watch_from_brand_and_model(monkeypatch):
    repo = StoryHighlightReferenceRepository()
    monkeypatch.setattr(repo, "list_active", lambda **_: [
        {"id": 1, "name": "Mido Baroncelli Heritage", "normalized_name": "mido baroncelli heritage", "product_url": "https://www.newstorerj.com.br/mido-baroncelli"},
        {"id": 2, "name": "Bulova Jet Star", "normalized_name": "bulova jet star", "product_url": "https://www.newstorerj.com.br/bulova-jet-star"},
    ])
    matches = repo.match_text(workspace_id="ws", tenant_id="newstore", text="No pulso aparece um Mido Baroncelli")
    assert [match.id for match in matches] == [1]
    assert matches[0].product_url.endswith("mido-baroncelli")


def test_does_not_guess_when_brand_only_matches_multiple_references(monkeypatch):
    repo = StoryHighlightReferenceRepository()
    monkeypatch.setattr(repo, "list_active", lambda **_: [
        {"id": 1, "name": "Mido Baroncelli", "normalized_name": "mido baroncelli", "product_url": "https://www.newstorerj.com.br/mido-baroncelli"},
        {"id": 2, "name": "Mido Ocean Star", "normalized_name": "mido ocean star", "product_url": "https://www.newstorerj.com.br/mido-ocean-star"},
    ])
    assert repo.match_text(workspace_id="ws", tenant_id="newstore", text="Do Mido que está no seu pulso") == []


def test_brand_only_can_use_single_configured_reference(monkeypatch):
    repo = StoryHighlightReferenceRepository()
    monkeypatch.setattr(repo, "list_active", lambda **_: [
        {"id": 1, "name": "Mido Baroncelli", "normalized_name": "mido baroncelli", "product_url": "https://www.newstorerj.com.br/mido-baroncelli"},
    ])
    assert repo.match_text(workspace_id="ws", tenant_id="newstore", text="Do Mido que está no seu pulso")[0].id == 1


@pytest.mark.asyncio
async def test_message_search_checks_highlight_reference_before_broad_search(monkeypatch):
    from app.catalog.retrieval.specific import resolve_products_from_message_text

    monkeypatch.setattr(
        "app.stories.story_highlight_references.current_workspace_reference",
        lambda *_args, **_kwargs: StoryHighlightReference(
            id=1, name="Mido Baroncelli", product_url="https://www.newstorerj.com.br/mido-baroncelli", matched_tokens=("mido",),
        ),
    )
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        return {"products": [{"id": "mido-1", "name": "Mido Baroncelli"}]}

    result = await resolve_products_from_message_text("Do Mido que está no seu pulso", execute_tool=execute)
    assert result[0]["id"] == "mido-1"
    assert calls[0][0] == "search_products"
    assert calls[0][1]["name"] == "baroncelli"
    assert calls[0][1]["brand"].casefold() == "mido"
