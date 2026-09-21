"""CAPTCHA solving with provider fallback.

SECURITY NOTE: any provider here is a THIRD PARTY that receives whatever is in
``challenge``. Never put profile PII in a challenge payload -- pass only the
site key, page URL and challenge image/audio. Provider credentials belong in
environment variables (see ``broker_guard.config``), never in this module.
"""


def solve(challenge: dict, providers: list) -> dict:
    attempts = []
    for provider in providers or []:
        name = getattr(provider, "__name__", "unknown")
        try:
            result = provider(challenge)
        except Exception as exc:
            attempts.append({"ok": False, "provider": name, "error": str(exc)})
            continue
        if not isinstance(result, dict):
            # A provider returning a bare token/None used to raise
            # AttributeError and abort the whole fallback chain.
            attempts.append({"ok": False, "provider": name, "error": "provider returned a non-dict result"})
            continue
        if not result.get("ok"):
            attempts.append(dict(result))
            continue
        out = dict(result)
        out["attempts"] = attempts
        return out
    return {"ok": False, "token": None, "fallback": "manual_queue", "attempts": attempts}


def solve_audio(audio_url, transcribe):
    try:
        transcript = transcribe(audio_url)
    except Exception:
        return {'ok': False, 'provider': 'whisper', 'token': None}
    if not isinstance(transcript, str):
        return {'ok': False, 'provider': 'whisper', 'token': None}
    token = transcript.strip().lower()
    if not token:
        return {'ok': False, 'provider': 'whisper', 'token': None}
    return {'ok': True, 'provider': 'whisper', 'token': token}


def solve_grid(challenge: dict, classify) -> dict:
    tiles = challenge.get('tiles') or []
    target = challenge.get('target', '')
    threshold = challenge.get('threshold', 0.5)
    selection = []
    max_confidence = 0.0
    for index, tile in enumerate(tiles):
        try:
            confidence = float(classify(tile, target))
        except Exception:
            confidence = 0.0
        if confidence > max_confidence:
            max_confidence = confidence
        if confidence >= threshold:
            selection.append(index)
    return {'ok': bool(selection), 'provider': 'vision', 'selection': selection,
            'low_confidence': max_confidence < 0.8}
