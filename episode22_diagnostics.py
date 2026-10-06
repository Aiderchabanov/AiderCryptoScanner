"""Temporary allowlisted logs only; no HTTP, database, or strategy mutations."""
import json
import logging
from decimal import Decimal


def emit(e, q, phase, before=None, after=None, new_levels=(), funding=None):
    if e.get('episode_id') != 22 or e.get('symbol') != 'CATEUSDT':
        return
    try:
        spread = Decimal(q['spread'])
        expansion = spread - Decimal(e['executable_spread_pct'])
        known = q.get('net_pnl') is not None
        nonnegative = Decimal(q['net_pnl']) >= 0 if known else None
        converged = abs(spread) <= Decimal('0.10')
        reason = ('SPREAD_NOT_CONVERGED' if not converged else
                  'UNKNOWN_SETTLEMENT_PNL' if not known else
                  'CONVERGED_BUT_NET_NEGATIVE' if not nonnegative else 'NONE')
        def value(x):
            if x is None:
                return 'UNKNOWN'
            if isinstance(x, bool):
                return x
            try:
                parsed=Decimal(str(x))
                return str(parsed) if parsed.is_finite() else 'UNKNOWN'
            except Exception:
                return 'UNKNOWN'
        snapshot = {
            'timestamp': q['at'], 'episode_id': 22, 'symbol': 'CATEUSDT',
            'status': 'OPEN_AT_OBSERVATION', 'phase': phase,
            'current_executable_spread': str(spread),
            'abs_current_executable_spread': str(abs(spread)),
            'entry_spread': e['executable_spread_pct'],
            'current_expansion_pp': str(expansion),
            'spot_exit_executable_price': value(q.get('spot_exit')),
            'futures_exit_executable_price': value(q.get('future_exit')),
            'spot_pnl': value(q.get('spot_pnl')),
            'futures_pnl': value(q.get('futures_pnl')),
            'trading_net_pnl': value(q.get('trading_net')),
            'funding_event_time': value((funding or {}).get('next_at')),
            'funding_rate': value((funding or {}).get('rate')),
            'funding_metadata_sampled_at': value((funding or {}).get('at')),
            'funding_metadata_source': 'EXISTING_PERSISTED_FUNDING_LIVE',
            'entry_funding_event_time': value(e.get('next_funding_at')),
            'entry_funding_rate': value(q.get('funding_rate_at_entry')),
            'funding_status': q.get('funding_status') if q.get('funding_status') in
                ('no settlement crossed','UNKNOWN_SETTLEMENT_PNL') else 'UNKNOWN',
            'funding_realized_usdt': value(q.get('funding_realized_usdt')),
            'full_net_pnl': value(q.get('net_pnl')),
            'close_spread_condition_met': converged,
            'known_net_available': known,
            'known_net_nonnegative': value(nonnegative),
            'auto_close_eligible': converged and known and nonnegative,
            'auto_close_block_reason': reason,
            'hysteresis_pp': '0.5', 'reset_threshold_pp': '0.5',
        }
        if before is not None and after is not None:
            old, new = set(before), set(after)
            created = 1 in new_levels
            snapshot.update({
                'spread_notified_before': sorted(old),
                'spread_notified_after': sorted(new),
                'plus_1_present_before': 1 in old,
                'plus_1_present_after': 1 in new,
                'plus_1_reset_triggered': 1 in old and expansion < Decimal('0.5'),
                'plus_1_new_event_created': created,
                'potential_duplicate_plus_1': created and 1 in old,
            })
        logging.info('Episode22 diagnostic: %s', json.dumps(snapshot, sort_keys=True))
    except Exception:
        # Diagnostics must never interrupt observation or trading decisions.
        logging.warning('Episode22 diagnostic unavailable; no strategy action')
