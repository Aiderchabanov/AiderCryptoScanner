"""Read-only spot/perpetual basis alerts. No order or transfer endpoint is used."""

import logging
import os
import time
from decimal import Decimal, ROUND_DOWN
import paper

FUTURES_BINANCE = 'https://fapi.binance.com'
LEG_USDT = Decimal('50')
RESERVE_USDT = Decimal('50')
EXIT_SLIPPAGE = Decimal(os.getenv('BASIS_EXIT_SLIPPAGE_PCT', '0.30')) / 100
MAX_BASIS_CANDIDATES = int(os.getenv('BASIS_MAX_CANDIDATES', '8'))
MIN_BASIS_NET = max(Decimal('0.5'), Decimal(os.getenv('BASIS_MIN_CONVERGENCE_PCT', '0.5')))
PRICE_BUFFER_PCT = Decimal('0.20')
last_alert = {}
api_blocked_until = {}


def down(value, step):
    if step is None or step <= 0:
        raise ValueError('Futures quantity step unavailable')
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def futures_markets(api):
    # A temporary ban on one futures host must not disable the other direction.
    b, g = [], []
    for exchange, load in (
        ('Binance', lambda: api.get_json(FUTURES_BINANCE + '/fapi/v1/ticker/bookTicker')),
        ('Gate', lambda: api.gate('/futures/usdt/tickers')),
    ):
        try:
            rows = load()
            if not isinstance(rows, list):
                raise ValueError('Futures tickers unavailable')
            if exchange == 'Binance':
                b = rows
            else:
                g = rows
        except Exception as exc:
            logging.warning('Basis %s futures market unavailable (%s)',
                            exchange, type(exc).__name__)
    return ({r['symbol']: r for r in b if r.get('symbol', '').endswith('USDT')},
            {r['contract'].replace('_', ''): r for r in g
             if r.get('contract', '').endswith('_USDT')})


def futures_meta(api, exchange, symbol):
    if exchange == 'Gate':
        contract = symbol[:-4] + '_USDT'
        info = api.cached(('basis-meta', exchange, symbol), 300,
                          lambda: api.gate('/futures/usdt/contracts/' + contract))
        if info.get('in_delisting') or info.get('status') not in (None, 'trading'):
            raise ValueError('Inactive futures contract')
        multiplier = api.dec(info.get('quanto_multiplier'))
        step = Decimal('0.001') if info.get('enable_decimal') else Decimal(1)
        minimum = api.dec(info.get('order_size_min'))
        if not multiplier or multiplier <= 0 or not minimum or minimum <= 0:
            raise ValueError('Futures contract size unavailable')
        return {'step': multiplier * step, 'min_qty': multiplier * minimum,
                'min_notional': Decimal(0), 'multiplier': multiplier,
                'funding': api.dec(info.get('funding_rate'))}
    rows = api.cached(('basis-meta', 'Binance'), 300,
                      lambda: api.get_json(FUTURES_BINANCE + '/fapi/v1/exchangeInfo'))
    info = next((r for r in rows['symbols'] if r.get('symbol') == symbol
                 and r.get('status') == 'TRADING' and r.get('contractType') == 'PERPETUAL'), None)
    if not info:
        raise ValueError('Inactive futures contract')
    filters = {r['filterType']: r for r in info['filters']}
    lot = filters.get('MARKET_LOT_SIZE') or filters.get('LOT_SIZE')
    if not lot or api.dec(lot.get('stepSize')) == 0:
        lot = filters.get('LOT_SIZE')
    notional = filters.get('MIN_NOTIONAL', {})
    return {'step': api.dec(lot['stepSize']), 'min_qty': api.dec(lot['minQty']),
            'min_notional': api.dec(notional.get('notional') or notional.get('minNotional') or 0),
            'multiplier': Decimal(1), 'funding': None}


def spot_minimum(api, exchange, symbol):
    if exchange == 'Gate':
        info = api.cached(('basis-spot', exchange, symbol), 300,
                          lambda: api.gate('/spot/currency_pairs/' + symbol[:-4] + '_USDT'))
        if info.get('trade_status') not in (None, 'tradable'):
            raise ValueError('Inactive spot pair')
        return api.dec(info.get('min_base_amount') or 0), api.dec(info.get('min_quote_amount') or 0)
    info = api.cached(('basis-spot', exchange, symbol), 300,
                      lambda: api.binance('/api/v3/exchangeInfo', {'symbol': symbol}))
    item = next((r for r in info['symbols'] if r.get('symbol') == symbol
                 and r.get('status') == 'TRADING'), None)
    if not item:
        raise ValueError('Inactive spot pair')
    filters = {r['filterType']: r for r in item['filters']}
    lot = filters.get('LOT_SIZE', {})
    notional = filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL') or {}
    return api.dec(lot.get('minQty') or 0), api.dec(notional.get('minNotional') or 0)


def futures_fee(api, exchange, symbol):
    def load():
        if exchange == 'Binance':
            row = api.get_json(FUTURES_BINANCE + '/fapi/v1/commissionRate',
                               params=api.binance_signed_params({'symbol': symbol}),
                               headers={'X-MBX-APIKEY': api.BINANCE_KEY})
            value = api.dec(row.get('takerCommissionRate'))
        else:
            contract = symbol[:-4] + '_USDT'
            try:
                row = api.gate('/futures/usdt/fee', {'contract': contract}, True)
                if isinstance(row, dict) and contract in row:
                    row = row[contract]
                value = api.dec(row.get('taker_fee')) if isinstance(row, dict) else None
            except Exception:
                value = None
            if value is None:
                # Gate's authenticated personal fee API also reports the
                # futures taker rate for the requested USDT settlement.
                row = api.gate('/wallet/fee', {'currency_pair': contract,
                                               'settle': 'USDT'}, True)
                value = api.dec(row.get('futures_taker_fee')) if isinstance(row, dict) else None
        if value is None or not 0 <= value < 1:
            raise ValueError('Personal futures fee unavailable')
        return value
    return api.cached(('basis-futures-fee', exchange, symbol), 60, load)


def futures_book(api, exchange, symbol, multiplier):
    if exchange == 'Binance':
        row = api.get_json(FUTURES_BINANCE + '/fapi/v1/depth',
                           params={'symbol': symbol, 'limit': 100})
        return row['asks'], row['bids']
    row = api.gate('/futures/usdt/order_book',
                   {'contract': symbol[:-4] + '_USDT', 'limit': 100})
    return ([[x['p'], abs(api.dec(x['s'])) * multiplier] for x in row['asks']],
            [[x['p'], abs(api.dec(x['s'])) * multiplier] for x in row['bids']])


def spot_cost(asks, quantity, api):
    remaining, spent = quantity, Decimal(0)
    for raw_price, raw_quantity in asks:
        price, available = api.dec(raw_price), api.dec(raw_quantity)
        if not price or not available or price <= 0 or available <= 0:
            return None
        take = min(remaining, available)
        spent += take * price
        remaining -= take
        if remaining <= Decimal('0.00000001'):
            return spent
    return None


def funding_expense(rate, direction, notional, crosses):
    if not crosses:
        return Decimal(0)
    if direction not in ('short', 'long'):
        raise ValueError('Unknown futures direction')
    # Positive funding is paid by longs; negative funding is paid by shorts.
    signed_cost = rate if direction == 'long' else -rate
    return max(signed_cost, Decimal(0)) * notional


def fresh_funding(api, exchange, symbol):
    """Fetch the contract's funding independently of cached market metadata."""
    if exchange == 'Binance':
        row = api.get_json(FUTURES_BINANCE + '/fapi/v1/premiumIndex', params={'symbol': symbol})
        rate = api.dec(row.get('lastFundingRate'))
        next_ms = api.dec(row.get('nextFundingTime'))
        next_at = float(next_ms / 1000) if next_ms is not None else None
    else:
        row = api.gate('/futures/usdt/contracts/' + symbol[:-4] + '_USDT')
        raw_rate = row.get('funding_rate_next')
        if raw_rate is None or raw_rate == '':
            raw_rate = row.get('funding_rate')
        rate = api.dec(raw_rate)
        next_seconds = api.dec(row.get('funding_next_apply'))
        next_at = float(next_seconds) if next_seconds is not None else None
    if rate is None or abs(rate) > Decimal('0.1') or next_at is None or next_at <= time.time():
        raise ValueError('Current funding rate or next settlement unavailable')
    return rate, next_at


def evaluate(api, symbol, spot_exchange, future_exchange, funding_hint=None):
    meta = futures_meta(api, future_exchange, symbol)
    buy_fee = api.fee(spot_exchange, symbol)
    perp_fee = futures_fee(api, future_exchange, symbol)
    asks, _ = api.orderbook(spot_exchange, symbol)
    _, bids = futures_book(api, future_exchange, symbol, meta['multiplier'])
    if not asks or not bids:
        return None
    spot_ask, perp_bid = api.dec(asks[0][0]), api.dec(bids[0][0])
    if not spot_ask or not perp_bid or spot_ask <= 0 or perp_bid <= spot_ask:
        return None
    quantity = down(min(LEG_USDT * (1 - buy_fee) / spot_ask,
                        LEG_USDT / perp_bid), meta['step'])
    if quantity <= 0 or quantity < meta['min_qty']:
        return None
    acquired = quantity / (1 - buy_fee)
    cost = spot_cost(asks, acquired, api)
    short_proceeds = api.sell_for_usdt(bids, quantity)
    if cost is None or short_proceeds is None or cost > LEG_USDT or short_proceeds > LEG_USDT:
        return None
    rules = api.spot_rules(spot_exchange, symbol, 'buy')
    if not api.order_size_ok(rules, acquired, cost) or short_proceeds < meta['min_notional']:
        return None
    if cost < LEG_USDT * Decimal('0.8') or short_proceeds < LEG_USDT * Decimal('0.8'):
        return None
    funding, next_funding = fresh_funding(api, future_exchange, symbol)
    now = time.time()
    crosses_funding = next_funding <= now + 60 * 60
    # Short pays negative funding; a possible positive receipt is not booked.
    funding_debit = funding_expense(funding, 'short', short_proceeds, crosses_funding)
    price = spot_ask
    spot_exit = quantity * price * (1 - EXIT_SLIPPAGE) * (1 - buy_fee)
    cover = quantity * price * (1 + EXIT_SLIPPAGE)
    open_fee = short_proceeds * perp_fee
    close_fee = cover * perp_fee
    price_buffer = cost * PRICE_BUFFER_PCT / 100
    projected = spot_exit + short_proceeds - cover - cost - open_fee - close_fee - funding_debit - price_buffer
    pct = projected / cost * 100
    return {'symbol': symbol, 'spot': spot_exchange, 'future': future_exchange,
            'quantity': quantity, 'spot_entry': cost / acquired,
            'future_entry': short_proceeds / quantity, 'spot_cost': cost,
            'future_notional': short_proceeds, 'spot_exit': price * (1 - EXIT_SLIPPAGE),
            'future_exit': price * (1 + EXIT_SLIPPAGE), 'projected': projected,
            'pct': pct, 'spot_fee': buy_fee, 'future_fee': perp_fee,
            'funding': funding, 'next_funding_at': next_funding,
            'funding_crosses_60m': crosses_funding, 'funding_filtered': funding <= 0,
            'funding_debit': funding_debit, 'price_buffer': price_buffer,
            'spot_ask': spot_ask, 'future_bid': perp_bid,
            'spot_slippage_usdt': max(Decimal(0), cost - acquired * spot_ask),
            'futures_slippage_usdt': max(Decimal(0), quantity * perp_bid - short_proceeds),
            'multiplier': meta['multiplier'],
            'reserve': RESERVE_USDT}


def scan(api):
    if not paper.storage_ready():
        return []  # Never alert without durable episode/checkpoint records.
    if not all((api.BINANCE_KEY, api.BINANCE_SECRET, api.GATE_KEY, api.GATE_SECRET)):
        return []
    spots = api.tickers()
    futures = futures_markets(api)
    shortlist = []
    for spot_name, future_name, spot_rows, perp_rows in (
        ('Binance', 'Gate', spots[0], futures[1]),
        ('Gate', 'Binance', spots[1], futures[0]),
    ):
        for symbol in spot_rows.keys() & perp_rows.keys():
            spot_price = api.dec(spot_rows[symbol].get('askPrice' if spot_name == 'Binance' else 'lowest_ask'))
            perp_price = api.dec(perp_rows[symbol].get('bidPrice' if future_name == 'Binance' else 'highest_bid'))
            if spot_price and perp_price and spot_price > 0 and perp_price > spot_price:
                shortlist.append(((perp_price / spot_price - 1), symbol, spot_name, future_name,
                                  perp_rows[symbol].get('funding_rate')))
    shortlist = sorted((item for direction in (('Binance', 'Gate'), ('Gate', 'Binance'))
                        for item in sorted((x for x in shortlist if x[2:4] == direction),
                                           reverse=True)[:MAX_BASIS_CANDIDATES]), reverse=True)
    found = []
    for _, symbol, spot, future, funding in shortlist:
        if time.time() < api_blocked_until.get(future, 0):
            continue
        try:
            item = evaluate(api, symbol, spot, future, funding)
            if item and item['pct'] >= MIN_BASIS_NET and not item['funding_filtered']:
                found.append(item)
            elif item and item['funding_filtered']:
                logging.info('Basis %s %s/%s REJECTED_NEGATIVE_FUNDING: nonpositive rate',
                             symbol, spot, future)
        except Exception as exc:
            response = getattr(exc, 'response', None)
            status = getattr(response, 'status_code', None)
            if status in (401, 403, 418, 429):
                retry_after = getattr(response, 'headers', {}).get('Retry-After', '') if response else ''
                delay = max(3600, int(retry_after)) if retry_after.isdigit() else 3600
                api_blocked_until[future] = time.time() + delay
            logging.warning('Basis skipped %s %s/%s: %s HTTP %s at %s', symbol, spot,
                            future, type(exc).__name__, status or '-',
                            (getattr(response, 'url', '') or '').split('?')[0])
    found.sort(key=lambda x: x['pct'], reverse=True)
    now = time.time()
    for item in found[:3]:
        key = ('basis', item['symbol'], item['spot'], item['future'])
        try:
            # Recheck directly before creating an episode or sending Telegram.
            rate, next_at = fresh_funding(api, item['future'], item['symbol'])
            if rate <= 0:
                logging.info('Basis %s %s/%s REJECTED_NEGATIVE_FUNDING at final check',
                             item['symbol'], item['spot'], item['future'])
                continue
            item['funding'], item['next_funding_at'] = rate, next_at
            item['funding_crosses_60m'] = next_at <= time.time() + 3600
            item['funding_debit'] = Decimal(0)  # Positive short funding is never booked as certain income.
            episode_id = paper.record(item)
            if episode_id is None:
                continue  # Same continuous spread episode.
            api.telegram(format_alert(item) + '\n' + paper.history_line(None, item['symbol']))
            last_alert[key] = now
        except Exception as exc:
            logging.warning('Basis alert unverified: persistent episode unavailable (%s)',
                            type(exc).__name__)
    return found


def format_alert(x):
    return (f"📊 Спот + бессрочный фьючерс: {x['symbol']}\n"
            f"Условный результат при схождении цен: {x['pct']:.2f}% "
            f"(≈ {x['projected']:.2f} USDT на {x['spot_cost']:.2f} USDT спота).\n"
            f"Купить спот {x['spot']}: {x['quantity']:.8g} по ≈ {x['spot_entry']:.8g} USDT.\n"
            f"Открыть шорт {x['future']}: тот же объём по ≈ {x['future_entry']:.8g} USDT.\n"
            f"Funding: +{x['funding']*100:.4f}%\n"
            f"Следующий funding: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(x['next_funding_at']))}\n"
            "Статус: положительный — условие выполнено.\n"
            f"Модель выхода при схождении около {x['spot_entry']:.8g}: "
            f"спот ≈ {x['spot_exit']:.8g}, покрытие шорта ≈ {x['future_exit']:.8g}.\n"
            f"Комиссии спот/фьючерс за сторону: {x['spot_fee']*100:.3f}% / "
            f"{x['future_fee']*100:.3f}%; учтены 4 сделки, проскальзывание "
            f"{EXIT_SLIPPAGE*100:.2f}% на каждой стороне выхода и возможная плата "
            f"funding ≈ {x['funding_debit']:.3f} USDT (short, ставка {x['funding']*100:.4f}%, "
            f"следующий расчёт {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(x['next_funding_at']))}; "
            "выплата лишь если позиция останется открыта к этому времени); дополнительный защитный резерв "
            f"{PRICE_BUFFER_PCT:.2f}% (≈ {x['price_buffer']:.2f} USDT).\n"
            f"Разместить примерно по {LEG_USDT:.0f} USDT на споте и в залоге фьючерса "
            f"плюс по {x['reserve']:.0f} USDT резерва на каждой бирже. "
            "Баланс, маржа и риск ликвидации не проверены. "
            "При расширении разницы или изменении funding возможен убыток. "
            "Это уведомление, без автоматических сделок.")
