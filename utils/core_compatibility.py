"""Validate explicit compatibility requirements for the external Hummingbot package.

Managed groups use the tested connector transport and native executor read model.
No wheel patch or unreleased upstream change is required.
"""

from typing import List, Tuple

# (import path, attribute, what it is for) — each one a field this API reads off the
# core and cannot substitute.
REQUIRED_CORE_SURFACE: List[Tuple[str, str, str]] = [
    (
        "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative:BinancePerpetualDerivative",
        "_api_get",
        "authenticated risk and order reconciliation",
    ),
    (
        "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative:BinancePerpetualDerivative",
        "_api_post",
        "stable-id order submission",
    ),
    (
        "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative:BinancePerpetualDerivative",
        "_api_delete",
        "entry cancellation",
    ),
    (
        "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative:BinancePerpetualDerivative",
        "exchange_symbol_associated_to_pair",
        "verified exchange symbol mapping",
    ),
    ("hummingbot.strategy_v2.models.executors_info:ExecutorInfo", "config", "managed executor read views"),
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
            "Managed execution and reconciliation cannot safely start without this interface."
        )
