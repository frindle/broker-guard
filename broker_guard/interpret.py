"""Build the interpretation prompt for a broker listing page."""
import json


def build_interpret_prompt(page_text: str, names: list[str], location: str | None) -> str:
    lines = [
        "Interpret this broker listing page.",
        "",
        "Page text:",
        page_text,
        "",
        "Names to interpret: " + ", ".join(names),
    ]
    if location:
        lines.append("Location: " + location)
    lines += [
        "",
        "Respond with JSON ONLY. No prose, no markdown fences.",
        "The JSON object must have exactly these keys:",
        "- listed (bool)",
        "- optout_fields (list)",
        "- verification (str)",
    ]
    return "\n".join(lines)


def parse_interpretation(raw: str) -> dict:
    """Parse the first {...} JSON object out of raw; never raises."""
    if not isinstance(raw, str):
        return {"error": "expected a string reply, got " + type(raw).__name__}
    start = raw.find("{")
    if start == -1:
        return {"error": "no complete {...} object found in reply"}
    depth = 0
    in_str = esc = False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start:i + 1])
                except ValueError as e:
                    return {"error": "invalid JSON object: " + str(e)}
                break
    else:
        return {"error": "no complete {...} object found in reply"}
    for key in ("listed", "optout_fields", "verification"):
        if key not in obj:
            return {"error": "missing required key: " + key}
    if not isinstance(obj["listed"], bool):
        return {"error": "'listed' must be a bool, got " + type(obj["listed"]).__name__}
    if not isinstance(obj["optout_fields"], list):
        return {"error": "'optout_fields' must be a list"}
    if not isinstance(obj["verification"], str):
        return {"error": "'verification' must be a str"}
    return obj
