def status_after_removal(current: str, result: dict) -> str:
    if current == 'pending' and result.get('success'):
        return 'submitted'
    return current


def needs_reverify(status: str, present_now: bool) -> bool:
    return status == 'confirmed' and bool(present_now)
