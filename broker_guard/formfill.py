def map_profile_to_form(profile: dict, field_map: dict) -> dict:
    """Map profile values onto form field names, reporting what could not be filled.

    Returns the filled fields plus a ``missing`` list. A form whose own field is
    literally named ``missing`` would collide with that report key, so it is
    rejected rather than silently shadowed.
    """
    if "missing" in field_map:
        raise ValueError("'missing' is reserved and cannot be a form field name")
    result = {}
    missing = []
    for form_field, profile_key in field_map.items():
        value = profile.get(profile_key)
        if isinstance(value, str):
            filled = bool(value.strip())
        else:
            filled = bool(value)
        if filled:
            result[form_field] = value
        else:
            missing.append(form_field)
    result["missing"] = missing
    return result
