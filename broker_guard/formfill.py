def map_profile_to_form(profile: dict, field_map: dict) -> dict:
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
