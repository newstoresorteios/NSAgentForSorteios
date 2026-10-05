"""Delivery failures that cannot recover by replaying the same envelope."""


def permanent_delivery_failure(info):
    return info.get('retryable') is False or info.get('error') in {
        'meta_authentication_failed', 'meta_page_access_token_missing',
        'meta_recipient_missing', 'meta_reply_too_long',
        'whatsapp_media_not_configured', 'whatsapp_media_sender_mismatch',
        'whatsapp_media_recipient_or_sender_missing', 'whatsapp_media_invalid_image',
        'whatsapp_media_partial_delivery', 'whatsapp_media_delivery_uncertain',
    }


def record_delivery_failure(info, *, outbox_id, provider, channel, exhausted=False):
    """Emit one actionable diagnostic, without recipient data or provider payloads."""
    from app.ops.observability import log_event
    permanent = permanent_delivery_failure(info)
    if permanent or exhausted:
        log_event('delivery.operator_action_required', {
            'severity': 'error', 'outbox_id': outbox_id, 'provider': provider,
            'channel': channel, 'permanent': permanent,
            'reason': ('channel_authentication' if info.get('error') in {
                'meta_authentication_failed', 'meta_page_access_token_missing'
            } else 'permanent_delivery' if permanent else 'attempts_exhausted'),
            'retry_automatically': False,
        })
