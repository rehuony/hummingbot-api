"""Keep Hummingbot's client order IDs valid for Unicode trading pairs.

Core embeds the first/last characters of each token in an order ID. Exchanges
such as Binance accept only ASCII in that field, even for Chinese symbols.
Sanitize the generated identifier, preserving its nonce, length and broker tag;
the trading pair sent to the exchange must remain unchanged.
"""

import re
from typing import Optional

from hummingbot.connector import exchange_py_base
from hummingbot.connector import utils as connector_utils

_core_get_new_client_order_id = connector_utils.get_new_client_order_id


def get_ascii_client_order_id(
    is_buy: bool, trading_pair: str, hbot_order_id_prefix: str = "", max_id_len: Optional[int] = None
) -> str:
    order_id = _core_get_new_client_order_id(is_buy, trading_pair, hbot_order_id_prefix, max_id_len)
    return re.sub(r"[^.A-Za-z0-9_:/-]", "_", order_id)


def install_client_order_id_compatibility() -> None:
    """Install before connectors start; cover ExchangePyBase's bound import too."""
    connector_utils.get_new_client_order_id = get_ascii_client_order_id
    exchange_py_base.get_new_client_order_id = get_ascii_client_order_id
