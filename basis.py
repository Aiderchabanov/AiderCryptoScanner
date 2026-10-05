"""Read-only spot/perpetual basis alerts. No order or transfer endpoint is used."""

import logging
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING
import paper
import binance_io
import entry_diagnostics as diagnostics

FUTURES_BINANCE = 'https://fapi.binance.com'
TARGET_LEG_USDT = Decimal('30')
LEG_USDT = TARGET_LEG_USDT
RESERVE_USDT = Decimal('50')
EXIT_SLIPPAGE = Decimal(os.getenv('BASIS_EXIT_SLIPPAGE_PCT', '0.30')) / 100
MAX_BASIS_CANDIDATES = int(os.getenv('BASIS_MAX_CANDIDATES', '8'))
MIN_BASIS_NET = Decimal('0.20')
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
                logging.info('Binance futures market OK: symbols=%s',sum(isinstance(r,dict) and r.get('symbol','').endswith('USDT') for r in rows))
            else:
                g = rows
        except Exception as exc:
            if exchange=='Binance':
                response=getattr(exc,'response',None)
                status=getattr(response,'status_code',None)
                message='active API cooldown (no HTTP request)' if isinstance(exc,RuntimeError) and exc.args==('Exchange API temporarily unavailable',) else ('invalid public ticker response' if isinstance(exc,ValueError) else 'public API request failed; see HTTP diagnostic')
                logging.warning('Basis Binance futures market unavailable: endpoint=/fapi/v1/ticker/bookTicker HTTP=%s exception=%s message=%s',status if status is not None else 'no_response',type(exc).__name__,message)
            else:
                logging.warning('Basis %s futures market unavailable (%s)',exchange,type(exc).__name__)
    return ({r['symbol']: r for r in b if r.get('symbol', '').endswith('USDT')},
            {r['contract'].replace('_', ''): r for r in g
             if r.get('contract', '').endswith('_USDT')})


def futures_meta(api, exchange, symbol):
    if exchange == 'MEXC':
        return api.mexc.futures_meta(symbol)
    if exchange == 'BingX':
        return api.bingx.futures_meta(symbol)
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
    notional = filters.get('MIN_NOTIONAL') or filters.get('NOTIONAL')
    if not lot or not notional:
        raise ValueError('Futures minimum order filters unavailable')
    return {'step': api.dec(lot['stepSize']), 'min_qty': api.dec(lot['minQty']),
            'min_notional': api.dec(notional.get('notional') if notional.get('notional') is not None else notional.get('minNotional')),
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
    if exchange == 'MEXC':
        return api.mexc.fee(symbol, futures=True)
    if exchange == 'BingX':
        return api.bingx.fee(symbol, futures=True)
    def load():
        if exchange == 'Binance':
            row = api.get_json(FUTURES_BINANCE + '/fapi/v1/commissionRate',
                               params=api.binance_signed_params({'symbol': symbol}),
                               headers={'X-MBX-APIKEY': api.BINANCE_KEY})
            value = api.dec(row.get('takerCommissionRate'))
        else:
            contract = symbol[:-4] + '_USDT'
            # Gate documents this authenticated endpoint for the personal
            # perpetual taker rate. The separate futures fee path returned 403
            # for this account and must not be retried before every scan.
            row = api.gate('/wallet/fee', {'currency_pair': contract,
                                           'settle': 'USDT'}, True)
            value = api.dec(row.get('futures_taker_fee')) if isinstance(row, dict) else None                
        if value is None or not 0 <= value < 1:
            raise ValueError('Personal futures fee unavailable')
        if exchange == 'Binance':
            logging.info('Binance Futures personal commission OK: HTTP status=200; taker_rate=%s',value)
        return value
    return api.cached(('basis-futures-fee', exchange, symbol), 3600 if exchange == 'Binance' else 60, load)


def futures_book(api, exchange, symbol, multiplier):
    if exchange == 'MEXC':
        return api.mexc.orderbook(symbol, futures=True)
    if exchange == 'BingX':
        return api.bingx.orderbook(symbol, futures=True)
    if exchange == 'Binance':
        row = api.get_json(FUTURES_BINANCE + '/fapi/v1/depth',
                           params={'symbol': symbol, 'limit': 50})
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
    if exchange == 'MEXC':
        return api.mexc.fresh_funding(symbol)
    if exchange == 'BingX':
        return api.bingx.fresh_funding(symbol)
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
        exc = ValueError('Current funding rate or next settlement unavailable')
        exc.missing_mandatory_data = (['MISSING_FUNDING'] if rate is None or abs(rate) > Decimal('0.1') else []) + (['MISSING_FUNDING_TIMESTAMP'] if next_at is None or next_at <= time.time() else [])
        raise exc
    return rate, next_at


def spread_category(raw, executable):
    # Both the best prices and executable depth must qualify; no last prices.
    if raw > 0 and executable > 0:
        return 'POSITIVE'
    if Decimal('-2') <= raw < 0 and Decimal('-2') <= executable < 0:
        return 'NEGATIVE'
    return None


def qualifies(item, now=None):
    import entry_policy
    return entry_policy.reason(item,now) is None


def evaluate(api, symbol, spot_exchange, future_exchange, funding_hint=None):
    token = diagnostics.STAGE.set('OTHER_MANDATORY_DATA')
    try:
        return _evaluate(api, symbol, spot_exchange, future_exchange, funding_hint)
    except Exception as exc:
        if not getattr(exc, 'missing_mandatory_data', None):
            exc.missing_mandatory_data = diagnostics.classify(exc)
        raise
    finally:
        diagnostics.STAGE.reset(token)


def _evaluate(api, symbol, spot_exchange, future_exchange, funding_hint=None):
    sampled_from = time.time()
    meta = diagnostics.call('MISSING_FUTURES_MARKET_PARAMS', futures_meta, api, future_exchange, symbol)
    buy_fee = diagnostics.call('MISSING_SPOT_FEE', api.fee, spot_exchange, symbol)
    perp_fee = diagnostics.call('MISSING_FUTURES_FEE', futures_fee, api, future_exchange, symbol)
    spot_book = diagnostics.call('MISSING_SPOT_DEPTH', api.orderbook, spot_exchange, symbol)
    future_book = diagnostics.call('MISSING_FUTURES_DEPTH', futures_book, api, future_exchange, symbol, meta['multiplier'])
    if getattr(api, 'multi_exchange', False):
        import virtual
        spot_book = diagnostics.call('MISSING_SPOT_DEPTH', virtual.checked_book, spot_book)
        future_book = diagnostics.call('MISSING_FUTURES_DEPTH', virtual.checked_book, future_book)
    asks, spot_bids = spot_book
    future_asks, bids = future_book
    if not asks or not bids:
        return None
    diagnostics.mark('MISSING_EXECUTABLE_PRICE')
    spot_ask, perp_bid = api.dec(asks[0][0]), api.dec(bids[0][0])
    if not spot_ask or not perp_bid or spot_ask <= 0 or perp_bid <= 0:
        return None
    rules = diagnostics.call('MISSING_SPOT_MARKET_PARAMS', api.spot_rules, spot_exchange, symbol, 'buy')
    # Step expresses allowed quantity precision; Futures size is rounded down by it.
    diagnostics.mark('MISSING_SPOT_MARKET_PARAMS')
    required=[meta.get('min_qty'),meta.get('step'),meta.get('min_notional'),rules.get('min_qty'),rules.get('min_quote'),rules.get('step')] if rules else []
    if len(required)!=6 or any(v is None or not Decimal(str(v)).is_finite() or Decimal(str(v))<0 for v in required) or meta['step']<=0 or rules['step']<=0:
        exc = ValueError('Mandatory minimum order / quantity precision unavailable')
        fields = [(meta, 'min_qty', 'MISSING_MIN_QTY'), (meta, 'step', 'MISSING_STEP_SIZE'), (meta, 'min_notional', 'MISSING_MIN_NOTIONAL'), (rules or {}, 'min_qty', 'MISSING_MIN_QTY'), (rules or {}, 'step', 'MISSING_STEP_SIZE'), (rules or {}, 'min_quote', 'MISSING_MIN_NOTIONAL')]
        exc.missing_mandatory_data = [label for row,key,label in fields if row.get(key) is None]
        raise exc
    if rules['min_quote']>LEG_USDT or meta['min_notional']>LEG_USDT or meta['min_qty']*perp_bid>LEG_USDT or rules['min_qty']*spot_ask>LEG_USDT:
        import entry_policy
        entry_policy.reject('REJECTED_MIN_ORDER_ABOVE_TARGET',{'symbol':symbol,'spot':spot_exchange,'future':future_exchange})
        logging.info('REJECTED_MIN_ORDER_ABOVE_TARGET: %s %s/%s target=%s USDT; required exchange minimum exceeds target',symbol,spot_exchange,future_exchange,LEG_USDT)
        return None
    diagnostics.mark('MISSING_EXECUTABLE_PRICE')
    quantity = down(min(LEG_USDT * (1 - buy_fee) / spot_ask,
                        LEG_USDT / perp_bid), meta['step'])
    if quantity <= 0 or quantity < meta['min_qty']:
        import entry_policy
        entry_policy.reject('REJECTED_MIN_ORDER_ABOVE_TARGET',{'symbol':symbol,'spot':spot_exchange,'future':future_exchange})
        logging.info('REJECTED_MIN_ORDER_ABOVE_TARGET: %s %s/%s target=%s USDT; Futures minQty/stepSize',symbol,spot_exchange,future_exchange,LEG_USDT)
        return None
    acquired = (quantity / (1 - buy_fee) / rules['step']).to_integral_value(rounding=ROUND_CEILING) * rules['step']
    cost = spot_cost(asks, acquired, api)
    short_proceeds = api.sell_for_usdt(bids, quantity)
    if cost is None or short_proceeds is None or cost > LEG_USDT or short_proceeds > LEG_USDT:
        return None
    if acquired < rules['min_qty'] or cost < rules['min_quote'] or short_proceeds < meta['min_notional']:
        import entry_policy
        entry_policy.reject('REJECTED_MIN_ORDER_ABOVE_TARGET',{'symbol':symbol,'spot':spot_exchange,'future':future_exchange})
        logging.info('REJECTED_MIN_ORDER_ABOVE_TARGET: %s %s/%s target=%s USDT; minimum size/notional fails at executable prices',symbol,spot_exchange,future_exchange,LEG_USDT)
        return None
    if not api.order_size_ok(rules, acquired, cost):
        return None
    if cost < LEG_USDT * Decimal('0.8') or short_proceeds < LEG_USDT * Decimal('0.8'):
        return None
    raw_spread = (perp_bid / spot_ask - 1) * 100
    executable_spread = ((short_proceeds / quantity) / (cost / acquired) - 1) * 100
    category = spread_category(raw_spread, executable_spread)
    if category is None:
        return None
    try:
        funding, next_funding = diagnostics.call('MISSING_FUNDING', fresh_funding, api, future_exchange, symbol)
    except Exception as exc:
        import entry_policy
        entry_policy.reject('REJECTED_UNKNOWN_FUNDING',{'symbol':symbol,'spot':spot_exchange,'future':future_exchange,'missing_mandatory_data':diagnostics.classify(exc)})
        exc.entry_rejection_counted=True
        raise
    now = time.time()
    crosses_funding = next_funding <= now + 60 * 60
    # Short pays negative funding; a possible positive receipt is not booked.
    funding_debit = funding_expense(funding, 'short', short_proceeds, crosses_funding)
    price = spot_ask
    # Model convergence to the current Spot reference; use actual exit-side
    # depth impact as a floor, in addition to the existing exit reserve.
    diagnostics.mark('MISSING_SPOT_DEPTH' if not spot_bids else 'MISSING_FUTURES_DEPTH')
    if not spot_bids or not future_asks:raise ValueError('Exit depth unavailable')
    exit_sale=api.sell_for_usdt(spot_bids,quantity)
    exit_cover=spot_cost(future_asks,quantity,api)
    diagnostics.mark('MISSING_SPOT_DEPTH' if exit_sale is None else 'MISSING_FUTURES_DEPTH')
    if exit_sale is None or exit_cover is None:raise ValueError('Exit depth unavailable')
    spot_exit_slip=max(EXIT_SLIPPAGE,1-exit_sale/(quantity*api.dec(spot_bids[0][0])))
    future_exit_slip=max(EXIT_SLIPPAGE,exit_cover/(quantity*api.dec(future_asks[0][0]))-1)
    spot_exit = quantity * price * (1 - spot_exit_slip) * (1 - buy_fee)
    cover = quantity * price * (1 + future_exit_slip)
    open_fee = short_proceeds * perp_fee
    close_fee = cover * perp_fee
    price_buffer = cost * PRICE_BUFFER_PCT / 100
    projected = spot_exit + short_proceeds - cover - cost - open_fee - close_fee - funding_debit - price_buffer
    pct = projected / cost * 100
    if time.time() - sampled_from > 30:
        exc = ValueError('Entry quotes expired')
        exc.missing_mandatory_data = ['STALE_SPOT_DATA','STALE_FUTURES_DATA']
        raise exc
    return {'entry_policy': 'positive_net_v1', 'verified_at': sampled_from, 'symbol': symbol, 'spot': spot_exchange, 'future': future_exchange,
            'category': category, 'raw_spread_pct': raw_spread,
            'executable_spread_pct': executable_spread,
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
            'paper_budget_usdt': LEG_USDT,
            'reserve': RESERVE_USDT}


def scan(api):
    token = diagnostics.begin()
    try:
        return _scan(api)
    finally:
        diagnostics.CYCLE.reset(token)


def _scan(api):
    if not paper.storage_ready():
        logging.info('basis scan: candidates=0, conditional alerts=0 (storage unavailable)')
        return []  # Never alert without durable episode/checkpoint records.
    if getattr(api, 'multi_exchange', False) is True:
        spots, futures = api.market_maps()
        directions = [(spot, future, spot_rows, perp_rows)
                      for spot, spot_rows in spots.items()
                      for future, perp_rows in futures.items()
                      if spot != future and api.credentials_ready(spot) and api.credentials_ready(future)]
    else:
        if not all((api.BINANCE_KEY, api.BINANCE_SECRET, api.GATE_KEY, api.GATE_SECRET)):
            logging.info('basis scan: candidates=0, conditional alerts=0 (read-only credentials unavailable)')
            return []
        spots = api.tickers()
        futures = futures_markets(api)
        directions = [('Binance', 'Gate', spots[0], futures[1]),
                      ('Gate', 'Binance', spots[1], futures[0])]
    available_directions = [(x[0], x[1]) for x in directions if x[2] and x[3]]
    logging.info('basis directions: active_directions=%s directions=%s', len(available_directions), available_directions)
    for spot, future in available_directions:
        diagnostics.CYCLE.get()['directions'].setdefault(spot+'->'+future, diagnostics.Counter())
    shortlist = []
    for spot_name, future_name, spot_rows, perp_rows in directions:
        for symbol in spot_rows.keys() & perp_rows.keys():
            spot_price = api.dec(spot_rows[symbol].get('lowest_ask' if spot_name == 'Gate' else 'askPrice'))
            perp_price = api.dec(perp_rows[symbol].get('highest_bid' if future_name == 'Gate' else 'bidPrice'))
            if spot_price and perp_price and spot_price > 0 and perp_price > 0 and (perp_price > spot_price or Decimal('-2') <= (perp_price / spot_price - 1) * 100 < 0):
                shortlist.append(((perp_price / spot_price - 1), symbol, spot_name, future_name,
                                  perp_rows[symbol].get('funding_rate')))
    shortlist = sorted((item for direction in ((x[0], x[1]) for x in directions)
                        for negative in (False, True)
                        for item in sorted((x for x in shortlist if x[2:4] == direction
                                            and (x[0] < 0) == negative),
                                           reverse=True)[:MAX_BASIS_CANDIDATES]), reverse=True)
    logging.info('MEXC scan: mexc_candidates=%s', sum('MEXC' in x[2:4] for x in shortlist))
    diagnostics.candidates(shortlist)
    found = []
    for _, symbol, spot, future, funding in shortlist:
        if time.time() < api_blocked_until.get(future, 0):
            continue
        try:
            item = evaluate(api, symbol, spot, future, funding)
            if item and qualifies(item):
                found.append(item)
            elif item:
                import entry_policy
                entry_policy.reject(entry_policy.reason(item),item)
        except Exception as exc:
            response = getattr(exc, 'response', None)
            status = getattr(response, 'status_code', None)
            if status in (401, 403, 418, 429):
                if future == 'Binance':
                    retry_after = getattr(response, 'headers', {}).get('Retry-After', '') if response is not None else ''
                    delay = binance_io.cooldown_seconds(status, retry_after)
                else:
                    # Preserve the existing Gate/BingX handling.
                    retry_after = getattr(response, 'headers', {}).get('Retry-After', '') if response else ''
                    delay = max(3600, int(retry_after)) if retry_after.isdigit() else 3600
                api_blocked_until[future] = time.time() + delay
            import entry_policy
            if not getattr(exc,'entry_rejection_counted',False):
                entry_policy.reject('REJECTED_UNAVAILABLE_MANDATORY_DATA',{'symbol':symbol,'spot':spot,'future':future,'raw_spread_pct':_ * 100,'missing_mandatory_data':diagnostics.classify(exc)})
            logging.warning('Basis skipped %s %s/%s: %s HTTP %s at %s', symbol, spot,
                            future, type(exc).__name__, status or '-',
                            (getattr(response, 'url', '') or '').split('?')[0])
    found.sort(key=lambda x: x['pct'], reverse=True)
    now = time.time()
    alerts = [item for category in ('POSITIVE', 'NEGATIVE')
              for item in [x for x in found if x.get('category', 'POSITIVE') == category][:3]]
    dispatched=0
    for item in alerts:
        key = ('basis', item['symbol'], item['spot'], item['future'])
        try:
            if item.get('executable_spread_pct', 0) > 5 or item.get('raw_spread_pct', 0) > 5:
                import virtual
                item = virtual.verified_entry(api, {'symbol':item['symbol'], 'spot_exchange':item['spot'], 'futures_exchange':item['future']})
            # Recheck directly before creating an episode or sending Telegram.
            rate, next_at = fresh_funding(api, item['future'], item['symbol'])
            if rate <= 0:
                logging.info('Basis %s %s/%s REJECTED_NEGATIVE_FUNDING at final check',
                             item['symbol'], item['spot'], item['future'])
                continue
            item['funding'], item['next_funding_at'] = rate, next_at
            if not qualifies(item):
                continue
            item['funding_crosses_60m'] = next_at <= time.time() + 3600
            item['funding_debit'] = Decimal(0)  # Positive short funding is never booked as certain income.
            episode_id = paper.record(item)
            if episode_id is None:
                continue  # Same continuous spread episode.
            import virtual
            item['used_capital']=virtual.capital(item['spot_cost'],item['future_notional'])
            logging.info('VIRTUAL OPEN verified: episode=%s symbol=%s Spot=%s Futures=%s expected_net_return_pct=%s expected_net_usdt=%s',episode_id,item['symbol'],item['spot'],item['future'],item['pct'],item['projected'])
            api.telegram(format_alert(item) + '\n' + paper.history_line(None, item['symbol'], item.get('category', 'POSITIVE')))
            last_alert[key] = now
            dispatched+=1
        except Exception as exc:
            logging.warning('Basis alert unverified: persistent episode unavailable (%s)',
                            type(exc).__name__)
    logging.info('basis scan: candidates=%s, conditional alerts=%s',len(shortlist),dispatched)
    diagnostics.completed(len(shortlist), dispatched)
    return found


def format_alert(x):
    category = x.get('category', 'POSITIVE')
    title = '🟢 SPOT → FUTURES POSITIVE' if category == 'POSITIVE' else '📉 SPOT → FUTURES NEGATIVE'
    remaining = max(0, int(x['next_funding_at'] - time.time()))
    raw = x.get('raw_spread_pct', (x.get('future_bid', x['future_entry']) / x.get('spot_ask', x['spot_entry']) - 1) * 100)
    executable = x.get('executable_spread_pct', (x['future_entry'] / x['spot_entry'] - 1) * 100)
    return (f"VIRTUAL OPEN — {title}\nEntry filter: POSITIVE NET ✅\nUsed capital: {x.get('used_capital', 'unknown')} USDT\nМонета: {x['symbol']}\n"
            f"Spot: {x['spot']}; Futures: {x['future']}\n"
            f"Размер каждой ноги: до ${LEG_USDT:.0f}; общий лимит капитала: 50% виртуального депозита.\n"
            f"Spot Ask: {x.get('spot_ask', x['spot_entry']):.8g}; Futures Bid: {x.get('future_bid', x['future_entry']):.8g} USDT.\n"
            f"Исполнимые цены по стакану: Spot {x['spot_entry']:.8g}; Futures {x['future_entry']:.8g}.\n"
            f"Фактические объёмы: {x['spot_cost']:.2f} USDT спот; "
            f"{x.get('future_notional', x['quantity'] * x['future_entry']):.2f} USDT фьючерс.\n"
            f"Начальный spread: {raw:+.4f}%; по стакану: {executable:+.4f}%.\n"
            f"Funding: +{x['funding']*100:.4f}%\n"
            f"Следующий funding: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(x['next_funding_at']))}\n"
            f"До funding: {remaining // 3600} ч {(remaining % 3600) // 60} мин {remaining % 60} сек.\n"
            "Статус: положительный — условие выполнено.\n"
            f"Комиссии spot/futures за сторону: {x['spot_fee']*100:.3f}% / {x['future_fee']*100:.3f}%. "
            f"Учтены четыре сделки; проскальзывание входа включено в цены; "
            f"резерв проскальзывания выхода {EXIT_SLIPPAGE*100:.2f}% на каждой стороне; "
            f"защитный резерв {PRICE_BUFFER_PCT:.2f}% ({x['price_buffer']:.3f} USDT); "
            f"расход funding {x['funding_debit']:.3f} USDT.\n"
            f"Модель результата при схождении после расходов: {x['pct']:+.2f}% "
            f"({x['projected']:+.3f} USDT). Это оценка, не реализованная прибыль; "
            "доход от будущего funding не включён.\n"
            "PAPER / VIRTUAL ONLY. Реальные ордера, переводы, выводы и усреднение не выполняются.")
