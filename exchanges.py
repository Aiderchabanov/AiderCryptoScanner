"""Routing adapter: legacy Binance/Gate implementations remain unchanged."""
import logging
import bingx
import mexc


class ScannerAPI:
    multi_exchange = True

    def __init__(self, legacy, bingx_client=None, mexc_client=None):
        self.mexc = mexc_client if mexc_client is not None else mexc.Client()
        self.legacy = legacy
        self.bingx = bingx_client if bingx_client is not None else bingx.Client()

    def __getattr__(self, name):
        return getattr(self.legacy, name)

    def credentials_ready(self, name):
        if name == 'MEXC':
            return self.mexc.enabled and self.mexc.credentials_ready and any(self.mexc.auth.values())
        if name == 'BingX':
            return self.bingx.enabled and self.bingx.credentials_ready
        return bool(getattr(self.legacy, name.upper() + '_KEY', '')
                    and getattr(self.legacy, name.upper() + '_SECRET', ''))

    def orderbook(self, exchange, symbol):
        if exchange == 'MEXC':
            return self.mexc.orderbook(symbol)
        if exchange == 'BingX':
            return self.bingx.orderbook(symbol)
        return self.legacy.orderbook(exchange, symbol)

    def fee(self, exchange, symbol):
        if exchange == 'MEXC':
            return self.mexc.fee(symbol)
        if exchange == 'BingX':
            return self.bingx.fee(symbol)
        return self.legacy.fee(exchange, symbol)

    def spot_rules(self, exchange, symbol, side):
        if exchange == 'MEXC':
            return self.mexc.spot_rules(symbol, side)
        if exchange == 'BingX':
            return self.bingx.spot_rules(symbol, side)
        return self.legacy.spot_rules(exchange, symbol, side)

    def networks(self, exchange, coin):
        if exchange == 'BingX':
            return self.bingx.networks(coin)
        return self.legacy.networks(exchange, coin)

    def market_maps(self):
        spots, futures = {}, {}
        # Separate requests: Gate outage must not hide Binance/BingX or vice versa.
        for name, load, normalize in (
            ('Binance', lambda: self.legacy.binance('/api/v3/ticker/bookTicker'),
             lambda r: r.get('symbol', '')),
            ('Gate', lambda: self.legacy.gate('/spot/tickers'),
             lambda r: r.get('currency_pair', '').replace('_', '')),
        ):
            if not self.credentials_ready(name):
                continue
            try:
                rows = load()
                if not isinstance(rows, list):
                    raise ValueError('ticker data unavailable')
                spots[name] = {normalize(r): r for r in rows if normalize(r).endswith('USDT')}
            except Exception as exc:
                logging.warning('Spot %s unavailable (%s)', name, type(exc).__name__)
        b, g = self.legacy.basis.futures_markets(self)
        if self.credentials_ready('Binance'):
            futures['Binance'] = b
        if self.credentials_ready('Gate'):
            futures['Gate'] = g
        if self.bingx.enabled:
            for target, market in ((spots, False), (futures, True)):
                try:
                    target['BingX'] = self.bingx.tickers(futures=market)
                except Exception as exc:
                    logging.warning('BingX %s markets unavailable (%s)',
                                    'Futures' if market else 'Spot', type(exc).__name__)
        if self.mexc.enabled and self.mexc.credentials_ready:
            for target, market, name in ((spots, False, 'spot'), (futures, True, 'futures')):
                if not self.mexc.auth[name]: continue
                try: target['MEXC'] = self.mexc.tickers(futures=market)
                except Exception as exc:
                    logging.warning('MEXC %s markets unavailable (%s)', name, type(exc).__name__)
        return spots, futures
