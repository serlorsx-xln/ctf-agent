"""Structured output schema for solver agents."""


def solver_output_json_schema() -> dict:
    """JSON schema for solver structured output — shared by Claude SDK and Codex.

    Only flag_found is allowed — solvers must keep working until they find a flag.
    No gave_up option forces persistent solving behavior.
    """
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["flag_found"]},
            "flag": {"type": "string"},
            "method": {"type": "string"},
        },
        "required": ["type", "flag", "method"],
        "additionalProperties": False,
    }
