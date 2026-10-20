"""Validate explicit compatibility requirements for the external Hummingbot package.

The current requirement list is empty: this API does not require a patched core or an
unreleased upstream change. Any future requirement must describe a field the API actually
uses and fail startup with an actionable message when that field is unavailable.
"""
from typing import List, Tuple

# (import path, attribute, what it is for) — each one a field this API reads off the
# core and cannot substitute.
REQUIRED_CORE_SURFACE: List[Tuple[str, str, str]] = [
    # No additional core fields are required beyond the current package interfaces.
]


def _resolve(path: str):
    module_path, _, name = path.partition(":")
    module = __import__(module_path, fromlist=[name])
    return getattr(module, name)


def require_core_surface() -> None:
    """Raise if the installed hummingbot is missing anything this API reads."""
    missing = []
    for path, attribute, purpose in REQUIRED_CORE_SURFACE:
        try:
            owner = _resolve(path)
        except (ImportError, AttributeError) as e:
            missing.append(f"  - {path} could not be imported ({e})")
            continue
        fields = getattr(owner, "model_fields", None)
        present = attribute in fields if fields is not None else hasattr(owner, attribute)
        if not present:
            missing.append(f"  - {path}.{attribute} — {purpose}")

    if missing:
        import hummingbot

        raise RuntimeError(
            "The installed hummingbot core is older than this API requires. Missing:\n"
            + "\n".join(missing)
            + f"\n\nInstalled hummingbot: {getattr(hummingbot, '__version__', 'unknown')}. "
            "Install a core that carries these fields (see environment.yml) and rebuild. "
            "Starting anyway would record every executor's pnl, fees and volume as zero."
        )
