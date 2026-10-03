"""Read-only connectivity probe. Never records episodes or sends Telegram."""
import json
from datetime import datetime, timezone
from decimal import Decimal
import bingx


def probe():
    client = bingx.Client()
    if not client.enabled:
        return {'mode': 'PAPER / VIRTUAL ONLY', 'error': 'Set BINGX_ENABLED=true'}
    report = {'mode': 'PAPER / VIRTUAL ONLY', 'observed_at': datetime.now(timezone.utc).isoformat(),
              'orders_transfers_withdrawals': 0, 'private_fees_checked': client.credentials_ready}
    for name, load in (
        ('spot_rules', lambda: client.spot_rules('BTCUSDT')),
        ('spot_bid_ask', lambda: client.book_ticker('BTCUSDT')),
        ('spot_depth', lambda: client.orderbook('BTCUSDT')),
        ('futures_rules', lambda: client.futures_meta('BTCUSDT')),
        ('futures_bid_ask', lambda: client.book_ticker('BTCUSDT', True)),
        ('futures_depth', lambda: client.orderbook('BTCUSDT', True)),
        ('funding', lambda: client.fresh_funding('BTCUSDT')),
    ):
        try:
            data = load()
            if 'depth' in name:
                asks, bids = data
                data = {'asks': len(asks), 'bids': len(bids), 'ask': asks[0][0], 'bid': bids[0][0]}
            report[name] = {'ok': True, 'data': data}
        except bingx.BingXUnavailable as exc:
            report[name] = {'ok': False, 'error': str(exc)}
    if client.credentials_ready:
        for name, load in (('spot_fee', lambda: client.fee('BTCUSDT')),
                           ('futures_fee', lambda: client.fee('BTCUSDT', True)),
                           ('networks', lambda: client.networks('USDT'))):
            try:
                data = load()
                # Restrict diagnostic output to public network/fee fields.
                report[name] = {'ok': True, 'data': data}
            except bingx.BingXUnavailable as exc:
                report[name] = {'ok': False, 'error': str(exc)}
    return report


if __name__ == '__main__':
    print(json.dumps(probe(), default=str, ensure_ascii=False, indent=2))
