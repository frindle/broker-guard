VALID_KINDS = ("automatable", "captcha", "photo_id", "kba")


def verification_kind(broker):
    kind = broker.get('verification')
    if not kind:
        return 'automatable'
    if kind in VALID_KINDS:
        return kind
    return 'manual_review'


def is_automatable(kind):
    return kind in ('automatable', 'captcha')
