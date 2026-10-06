"""Versioned PAPER entry policy; rejection counters contain no credentials."""
import logging,math,time
from decimal import Decimal,InvalidOperation
from contextlib import nullcontext

VERSION='positive_net_v1'
MIN_RETURN=Decimal('0.20')

def reason(item,now=None):
    now=time.time() if now is None else now
    try:
        funding=Decimal(str(item['funding']));next_at=float(item['next_funding_at'])
        if not funding.is_finite() or not math.isfinite(next_at) or next_at<=now:return 'REJECTED_UNKNOWN_FUNDING'
        if funding<=0:return 'REJECTED_NEGATIVE_FUNDING'
    except (KeyError,TypeError,ValueError,InvalidOperation):return 'REJECTED_UNKNOWN_FUNDING'
    try:
        value=Decimal(str(item['pct']))
        if not value.is_finite():return 'REJECTED_UNKNOWN_ENTRY_COST'
        if value<=0:return 'REJECTED_NON_POSITIVE_NET_ENTRY'
        if value<MIN_RETURN:return 'REJECTED_LOW_EXPECTED_NET_RETURN'
    except (KeyError,TypeError,ValueError,InvalidOperation):return 'REJECTED_UNKNOWN_ENTRY_COST'
    return None

def reject(code,item=None,path=None,db=None):
    item=item or {}
    import entry_diagnostics
    entry_diagnostics.record(code,item)
    try:
        diagnostic_row = entry_diagnostics.rejection_snapshot(code,item)
        entry_diagnostics.retain_rejection(diagnostic_row)
    except Exception as exc:
        diagnostic_row = None
        logging.warning('Entry calculation diagnostic unavailable (%s)',type(exc).__name__)
    logging.info('Entry rejection: %s symbol=%s Spot=%s Futures=%s expected_net_return_pct=%s',code,item.get('symbol','-'),item.get('spot','-'),item.get('future','-'),item.get('pct','unknown'))
    try:
        import paper,virtual
        if db is None and path is None and not paper.storage_ready():return
        with (nullcontext(db) if db is not None else paper.session(path)) as connection:
            virtual.ensure(connection)
            connection.execute("INSERT INTO virtual_meta (name,value) VALUES (?,'1') ON CONFLICT (name) DO UPDATE SET value=CAST(CAST(virtual_meta.value AS BIGINT)+1 AS TEXT)",('entry_rejection:'+code,))
            if diagnostic_row is not None:
                try:
                    entry_diagnostics.persist_rejection(connection,diagnostic_row)
                except Exception as exc:
                    logging.warning('Entry calculation persistence unavailable (%s)',type(exc).__name__)
    except Exception as exc:
        logging.warning('Entry rejection counter unavailable (%s)',type(exc).__name__)
