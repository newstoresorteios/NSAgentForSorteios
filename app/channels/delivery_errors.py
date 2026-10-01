"""Delivery failures that cannot recover by replaying the same envelope."""


def permanent_delivery_failure(info):
    return info.get('retryable') is False or info.get('error') in {
        'meta_authentication_failed', 'meta_page_access_token_missing',
        'meta_recipient_missing', 'meta_reply_too_long',
    }
