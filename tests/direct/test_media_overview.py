import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from app.direct.catalog import all_ready_products
from app.direct.tools import DirectTools
from app.models import IncomingMessage


@pytest.mark.asyncio
async def test_overview_includes_last_page_and_resolves_selected_photo():
    products = [{'name': f'Model {i}', 'url': f'https://www.newstorerj.com/p{i}',
                 'image_url': f'https://images.tcdn.com.br/p{i}.jpg'} for i in range(97)]
    products[-1]['name'] = 'Seiko Verde match beyond first pages'
    async def page(query, *, offset, limit, snapshot_id):
        assert query == 'pronta entrega' and limit == 50
        assert snapshot_id == (None if offset == 0 else 'a'*32)
        return dict(complete=True, snapshot_id='a'*32, total=97, products=products[offset:offset+limit],
                    has_more=offset+limit<97, next_offset=offset+limit)
    adapter = NS(search_ready_delivery=AsyncMock(side_effect=page))
    tools = DirectTools(incoming=IncomingMessage(), history=[], documents=[], adapter=adapter)
    overview = await tools.execute('compare_ready_delivery_catalog', '{}')
    assert overview['total'] == 97 and len(overview['candidates']) == 97
    selected = overview['candidates'][-1]
    assert selected['name'].startswith('Seiko Verde')
    assert 'url' not in selected
    result = await tools.execute('get_ready_delivery_candidate', json.dumps({'candidate_ids':[selected['candidate_id']]}))
    assert result['products'] == [dict(products[-1], candidate_id=selected['candidate_id'])]
    photo = await tools.execute('prepare_product_image', json.dumps({'candidate_id': selected['candidate_id']}))
    assert photo['image_attached_to_reply']
    assert adapter.search_ready_delivery.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['partial', 'missing', 'snapshot', 'total', 'offset'])
async def test_overview_never_presents_incomplete_catalog_as_complete(corruption):
    first = dict(complete=True, snapshot_id='a'*32, total=2,
                 products=[{'url':'https://www.newstorerj.com/1'}], has_more=True, next_offset=1)
    second = dict(complete=True, snapshot_id='a'*32, total=2,
                  products=[{'url':'https://www.newstorerj.com/2'}], has_more=False)
    if corruption == 'partial': second['complete'] = False
    if corruption == 'missing': second['products'] = []
    if corruption == 'snapshot': second['snapshot_id'] = 'b'*32
    if corruption == 'total': second['total'] = 3
    if corruption == 'offset': first['next_offset'] = 0
    with pytest.raises(ValueError):
        await all_ready_products(NS(search_ready_delivery=AsyncMock(side_effect=[first, second])))


@pytest.mark.asyncio
async def test_video_frames_and_audio_reach_same_agent_content_and_cleanup(monkeypatch, tmp_path):
    from app.direct.pipeline import input_content
    import app.direct.media as media
    path = tmp_path/'video.mp4'
    path.write_bytes(b'fixture')
    file = NS(path=path, content_type='video/mp4', close=Mock())
    monkeypatch.setattr(media, 'download_story_media_file', AsyncMock(return_value=file))
    def frames(path, *, max_frames, frame_times):
        frame_times.extend([0.5, 3.0])
        return [b'frame-one', b'frame-two']
    monkeypatch.setattr(media, 'extract_video_frames_best_effort', frames)
    monkeypatch.setattr(media, 'transcribe_story_video', AsyncMock(return_value=NS(status='transcribed', transcript='Quero o segundo relógio')))
    content = await input_content(IncomingMessage(text='Qual é esse?', image_url='https://cdninstagram.com/video', attachment_type='video'))
    assert sum(p['type']=='input_image' for p in content) == 2
    text = '\n'.join(p['text'] for p in content if p['type']=='input_text')
    assert '0.50s' in text and '3.00s' in text and 'Quero o segundo' in text
    file.close.assert_called_once()


@pytest.mark.asyncio
async def test_video_decoder_failure_keeps_transcript_without_claiming_vision(monkeypatch, tmp_path):
    import app.direct.media as media
    monkeypatch.setattr(media, 'extract_video_frames_best_effort', lambda *a, **k: [])
    monkeypatch.setattr(media, 'transcribe_story_video', AsyncMock(return_value=NS(status='transcribed', transcript='Modelo azul')))
    parts = await media.prepared_file_content(NS(path=tmp_path/'x', content_type='video/mp4'))
    assert not any(p['type']=='input_image' for p in parts)
    assert any('Não foi possível observar' in p['text'] for p in parts)
    assert any('Modelo azul' in p['text'] for p in parts)


@pytest.mark.asyncio
@pytest.mark.parametrize('story_url', ['https://cdninstagram.com/expired', None])
async def test_expired_story_refresh_preserves_story_priority_and_thumbnail_limit(monkeypatch, story_url):
    from pydantic import SecretStr
    from app.stories.instagram_story_models import InstagramStoryContext
    import app.direct.media as media
    story = InstagramStoryContext(provider='meta', story_media_id='123', media_type='video',
        story_media_url_private=SecretStr(story_url) if story_url else None,
        story_thumbnail_url_private=SecretStr('https://cdninstagram.com/thumb'))
    fresh = story.model_copy(update={'story_media_url_private':SecretStr('https://cdninstagram.com/fresh')})
    download = AsyncMock(side_effect=ValueError('unavailable'))
    monkeypatch.setattr(media, 'download_story_media_file', download)
    hydrate = AsyncMock(return_value=fresh)
    monkeypatch.setattr('app.stories.instagram_story_service._hydrate_story_media', hydrate)
    monkeypatch.setattr('app.core.remote_media.download_trusted_media', AsyncMock(return_value=(b'thumb','image/jpeg')))
    parts = await media.visual_content(IncomingMessage(instagram_story=story,
                    image_url='https://cdninstagram.com/unrelated-dm'))
    assert [call.args[0] for call in download.await_args_list] == [story.operational_media_url() or '', fresh.operational_media_url()]
    assert any('Somente miniatura' in p.get('text','') for p in parts)
    assert sum(p['type']=='input_image' for p in parts) == 1


@pytest.mark.asyncio
async def test_unknown_or_conflicting_photo_ids_do_not_send():
    from app.direct.catalog import candidate_id
    product = dict(name='Real', url='https://www.newstorerj.com/real', image_url='https://images.tcdn.com.br/real.jpg')
    tools = DirectTools(incoming=IncomingMessage(), history=[], documents=[], products=[product])
    for args in ({'candidate_id':'0'*16}, {'candidate_id':candidate_id(product), 'product_url':'https://www.newstorerj.com/other'}):
        assert not (await tools.execute('prepare_product_image', json.dumps(args)))['ok']
    assert tools.outbound_image_url is None
