"""atrium-fp-pipeline package entry point (v1.0.0)."""
from .fire_protection_pipeline import (
    fire_protection_pipeline,
    DEFAULTS,
    TOOL_ID,
    TOOL_NAME,
    TOOL_VERSION,
)


def run(**kwargs):
    return fire_protection_pipeline(**kwargs)


__all__ = [
    "fire_protection_pipeline",
    "run",
    "DEFAULTS",
    "TOOL_ID",
    "TOOL_NAME",
    "TOOL_VERSION",
]
