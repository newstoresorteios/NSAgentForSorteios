"""Published/manual associations are evidence; automatic visual matches are not."""
def story_references(incoming, *, workspace, tenant, query):
    from app.stories.story_highlight_references import StoryHighlightReferenceRepository
    from app.stories.story_product_repository import StoryProductRepository
    references = []
    story = incoming.instagram_story
    if story and story.story_media_id and story.instagram_account_id:
        row = StoryProductRepository().get_by_story(tenant_id=tenant, provider=story.provider,
            instagram_account_id=story.instagram_account_id, story_media_id=story.story_media_id)
        if (row and row.confirmed_by and row.match_source in {'manual', 'publication_metadata'}
                and row.match_status in {'matched', 'manually_confirmed'} and row.product_id):
            references.append({'kind': 'confirmed_publication', 'product_id': row.product_id,
                'variant_id': row.variant_id, 'source': row.match_source,
                'instruction': 'Identidade confirmada na publicação; consultar dados atuais, não inferir preço/estoque.'})
    for ref in StoryHighlightReferenceRepository().match_text(workspace_id=workspace, tenant_id=tenant, text=query):
        references.append({'kind': 'operator_highlight_reference', 'name': ref.name, 'url': ref.product_url,
                           'instruction': 'Referência cadastrada; não comprova disponibilidade atual.'})
    return {'ok': True, 'references': references}
