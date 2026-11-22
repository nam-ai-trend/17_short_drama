"""Review whether synopsis alternatives vary in structure and opening device."""

from typing import Any, Dict, List


def review_proposals(proposals: Dict[str, Any]) -> Dict[str, List[str]]:
    items = proposals.get("items") or []
    codes: List[str] = []
    if not items:
        return {"codes": codes}

    if len(items) != 3:
        codes.append("option_count")

    for field, repeated_code in (("genre_engine", "repeated_engine"), ("hook_type", "repeated_hook")):
        values = [str(item.get(field, "")).strip().lower() for item in items]
        if any(not value for value in values):
            codes.append("unclassified_options")
        elif len(set(values)) != len(values):
            codes.append(repeated_code)

    if any(not str(item.get("hook", "")).strip() for item in items):
        codes.append("missing_hook")

    for field, code in (
        ("protagonist_choice", "missing_protagonist_choice"),
        ("reveal_evidence", "missing_reveal_evidence"),
        ("payoff_action", "missing_payoff_action"),
    ):
        if any(not str(item.get(field, "")).strip() for item in items):
            codes.append(code)
    return {"codes": list(dict.fromkeys(codes))}
