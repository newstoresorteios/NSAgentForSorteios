"""A visual hypothesis is useful context, never a verified catalog binding."""
import re

from pydantic import ValidationError

from app.catalog.retrieval.text import fold_text
from app.stories.story_identity_verifier import IdentityReview
from app.stories.story_selection import color_family


async def probable_identity(*, analysis, worker_evidence, execute_tool):
    # Retain the original scene: a scoped crop must not hide competing watches.
    if (not worker_evidence or analysis.watch_count != 1 or analysis.multiple_products
            or len(analysis.product_regions) != 1 or analysis.image_quality == 'poor'):
        return None
    try:
        review = IdentityReview.model_validate({'checks': worker_evidence.get('identity_reviews') or []})
    except (ValidationError, TypeError):
        return None
    candidates = worker_evidence.get('candidates') or []
    ids = {str(c.get('product_id')) for c in candidates if c.get('product_id')}
    checks = [c for c in review.checks if c.region_index == 0]
    # Missing/uncertain/duplicate reviews cannot rule out a competing variant.
    if (not ids or len(checks) != len(ids) or {c.product_id for c in checks} != ids
            or len({c.product_id for c in checks}) != len(checks)
            or any(c.verdict == 'uncertain' or
                   (c.verdict == 'different' and (not c.conflicts or c.matches_catalog_photo)) for c in checks)):
        return None
    consistent = [c for c in checks if c.verdict == 'consistent']
    if len(consistent) != 1:
        return None
    check = consistent[0]
    region = analysis.product_regions[0]
    frames = set(check.visual_support_frame_indexes)
    if (check.conflicts or not check.matches_catalog_photo
            or len(frames) < (2 if analysis.media_type == 'video' else 1)
            or not frames.issubset(set(region.frame_indexes))
            or any(i < 0 or i >= analysis.frames_analyzed for i in frames)
            or len({f.strip().casefold() for f in check.distinguishing_features if f.strip()}) < 3):
        return None
    brand = (region.brand_hypothesis or '').strip()
    model = (region.reference_hypothesis or '').strip()
    # Only a short model label corroborated by the catalog enters the reply.
    if not brand or not model or not re.fullmatch(r'[\w .+\-/]{2,100}', brand + ' ' + model):
        return None
    try:
        product = await execute_tool('get_product', {'product_id': check.product_id})
    except Exception:
        return None
    if (not isinstance(product, dict) or product.get('error')
            or str(product.get('id')) != check.product_id):
        return None
    name_tokens = set(re.findall(r'\w+', fold_text(product.get('name') or '')))
    hint_tokens = set(re.findall(r'\w+', fold_text(brand + ' ' + model)))
    color = color_family(region.dial_color)
    if (not hint_tokens.issubset(name_tokens) or color not in
            {'azul', 'preto', 'branco', 'verde', 'rosa', 'cinza', 'prata', 'dourado', 'marrom'}
            or color not in {color_family(t) for t in name_tokens}):
        return None
    label = model if fold_text(model).startswith(fold_text(brand) + ' ') else brand + ' ' + model
    return {'model_label': label, 'dial_color': color,
            'certainty': 'probable', 'exact_identity_confirmed': False,
            'commercial_authority': False, 'frame_indexes': sorted(frames),
            'method': 'independent_catalog_photo_comparison',
            'reply': f'Pelo vídeo, parece ser o {label}, de mostrador {color}. '
                     'A comparação visual é compatível, mas ainda não confirmei a referência exata. '
                     'Para confirmar a versão, pode enviar um close do mostrador ou a referência?'
                     if analysis.media_type == 'video' else
                     f'Pela imagem, parece ser o {label}, de mostrador {color}. '
                     'Ainda preciso de um close do mostrador ou da referência para confirmar a versão exata.'}
