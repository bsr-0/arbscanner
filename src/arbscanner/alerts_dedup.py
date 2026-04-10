"""Alert deduplication for arb opportunities.

The arb scanner re-runs every ~30 seconds and repeatedly surfaces the same
opportunities while they persist on the order books. Without dedup, the
Telegram/Discord alert pipeline fires the same notification over and over,
which drowns out new signal.

Design
------
This module implements an in-memory TTL cache of recently-alerted opportunity
fingerprints. A fingerprint is a canonical string derived from
``(poly_market_id, kalshi_market_id, direction)`` — it identifies the arb
independent of timestamp or price snapshot.

When ``should_alert(opp)`` is called:

1. The opportunity's fingerprint is looked up in the cache.
2. If absent (or expired), the alert fires and the fingerprint is recorded
   with an expire time of ``now + ttl_seconds`` and the current ``net_edge``.
3. If present and still within TTL, the alert is suppressed UNLESS the
   ``net_edge`` has moved by more than ``edge_delta`` from the last-recorded
   value. This lets users see materially improving opportunities without
   suffering constant spam on static ones.
4. Whenever we fire, the entry is refreshed so the TTL window slides forward.

Memory is bounded by ``max_entries``; when the cap is hit we evict the
oldest-expiring entries (an approximation of LRU that's cheap to compute).
A background ``prune()`` call drops expired fingerprints.

The cache is guarded by a ``threading.Lock`` so it is safe to call from
multiple scanner threads. Only the Python standard library is used.

This module is intentionally standalone — wiring it into ``alerts.py``
happens in a separate change.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List

from arbscanner.models import ArbOpportunity


def opportunity_fingerprint(opp: ArbOpportunity) -> str:
    """Return a canonical identifier for an arb opportunity.

    The fingerprint is independent of timestamp, price snapshot, and
    available size — it only encodes *which* arb this is (the two markets
    and the direction). Two opportunities with the same fingerprint are
    treated as "the same alert" for dedup purposes.
    """
    return f"{opp.poly_market_id}|{opp.kalshi_market_id}|{opp.direction}"


class AlertDeduper:
    """In-memory TTL cache that suppresses repeat alerts for the same arb.

    Parameters
    ----------
    ttl_seconds:
        How long (in seconds) to suppress a repeat alert for the same
        fingerprint. Defaults to 5 minutes.
    max_entries:
        Maximum number of fingerprints to keep in memory. When exceeded,
        the oldest-expiring entries are evicted.
    edge_delta:
        If the ``net_edge`` of an opportunity has moved by strictly more
        than this (absolute) amount since the last alert, a new alert
        fires even within the TTL window. Defaults to 0.005 (0.5pp).
    """

    def __init__(
        self,
        ttl_seconds: float = 300,
        max_entries: int = 10000,
        edge_delta: float = 0.005,
    ) -> None:
        self.ttl_seconds: float = float(ttl_seconds)
        self.max_entries: int = int(max_entries)
        self.edge_delta: float = float(edge_delta)

        # fingerprint -> (expire_time_epoch, last_net_edge)
        self._entries: Dict[str, tuple[float, float]] = {}
        self._lock: threading.Lock = threading.Lock()

        # Observability counters.
        self._pruned_total: int = 0
        self._suppressed_total: int = 0

    # ------------------------------------------------------------------ core

    def should_alert(self, opp: ArbOpportunity) -> bool:
        """Decide whether to fire an alert for ``opp`` and record the decision.

        Returns True if:
          * this fingerprint has never been alerted,
          * the previous alert for this fingerprint has expired, or
          * the ``net_edge`` has moved by more than ``edge_delta`` since
            the last alert.

        Otherwise returns False. In all cases where True is returned, the
        cache is updated so the fingerprint's TTL window is reset and the
        new ``net_edge`` is recorded.
        """
        fingerprint = opportunity_fingerprint(opp)
        now = time.monotonic()
        new_expire = now + self.ttl_seconds

        with self._lock:
            existing = self._entries.get(fingerprint)

            fire = False
            if existing is None:
                fire = True
            else:
                expire_time, last_edge = existing
                if expire_time <= now:
                    # Previous alert has aged out.
                    fire = True
                elif abs(opp.net_edge - last_edge) > self.edge_delta:
                    # Edge moved enough to be worth re-alerting.
                    fire = True

            if not fire:
                self._suppressed_total += 1
                return False

            # Record / refresh the entry.
            self._entries[fingerprint] = (new_expire, opp.net_edge)

            # Enforce cap. We evict after the insert so a brand-new
            # fingerprint cannot be evicted by itself.
            if len(self._entries) > self.max_entries:
                overflow = len(self._entries) - self.max_entries
                self._evict_oldest(overflow)

            return True

    def filter(self, opps: List[ArbOpportunity]) -> List[ArbOpportunity]:
        """Return the subset of ``opps`` that should be alerted right now.

        Convenience wrapper around :meth:`should_alert`. Order is preserved.
        """
        return [o for o in opps if self.should_alert(o)]

    # ------------------------------------------------------------ maintenance

    def prune(self) -> int:
        """Drop expired entries. Returns the number of entries pruned."""
        now = time.monotonic()
        with self._lock:
            expired = [fp for fp, (exp, _) in self._entries.items() if exp <= now]
            for fp in expired:
                del self._entries[fp]
            self._pruned_total += len(expired)
            return len(expired)

    def _evict_oldest(self, n: int) -> None:
        """Evict the ``n`` entries with the earliest expire_time.

        Caller MUST hold ``self._lock``. This is an approximation of LRU
        that's O(k log k) in the number of entries — fine at the scale
        of ~10k fingerprints.
        """
        if n <= 0 or not self._entries:
            return
        # Sort (fingerprint, (expire, edge)) by expire ascending; take n.
        victims = sorted(self._entries.items(), key=lambda kv: kv[1][0])[:n]
        for fp, _ in victims:
            del self._entries[fp]

    # ---------------------------------------------------------------- stats

    def stats(self) -> dict:
        """Return a snapshot of deduper counters and current cache size."""
        with self._lock:
            return {
                "total_entries": len(self._entries),
                "pruned_total": self._pruned_total,
                "suppressed_total": self._suppressed_total,
            }


# ---------------------------------------------------------------------- demo

if __name__ == "__main__":
    # Tiny smoke test that exercises the main code paths without hitting
    # any network. Run with:  python -m arbscanner.alerts_dedup
    from datetime import datetime

    def make_opp(
        poly_id: str = "POLY-1",
        kalshi_id: str = "KX-1",
        direction: str = "poly_yes_kalshi_no",
        net_edge: float = 0.02,
    ) -> ArbOpportunity:
        return ArbOpportunity(
            poly_title="Will the Fed cut rates in June?",
            kalshi_title="KXFEDCUT-26JUN",
            poly_market_id=poly_id,
            kalshi_market_id=kalshi_id,
            direction=direction,
            poly_price=0.52,
            kalshi_price=0.46,
            gross_edge=0.03,
            net_edge=net_edge,
            available_size=100.0,
            expected_profit=net_edge * 100.0,
            timestamp=datetime.now(),
        )

    deduper = AlertDeduper(ttl_seconds=1.0, max_entries=5, edge_delta=0.005)

    opp = make_opp()
    print("fingerprint:", opportunity_fingerprint(opp))

    # First time: should fire.
    print("first call   ->", deduper.should_alert(opp))  # True
    # Immediate repeat, same edge: suppressed.
    print("repeat same  ->", deduper.should_alert(opp))  # False
    # Tiny edge change under delta: still suppressed.
    print("tiny change  ->", deduper.should_alert(make_opp(net_edge=0.021)))  # False
    # Big edge change: re-fires.
    print("big change   ->", deduper.should_alert(make_opp(net_edge=0.05)))  # True

    # Different direction on same markets: distinct fingerprint.
    other = make_opp(direction="poly_no_kalshi_yes")
    print("other dir    ->", deduper.should_alert(other))  # True

    # Batch filter: only new/changed fire.
    batch = [
        make_opp(poly_id="POLY-2"),
        make_opp(poly_id="POLY-2"),  # dup in same batch -> suppressed
        make_opp(poly_id="POLY-3"),
    ]
    fired = deduper.filter(batch)
    print("batch fired  ->", len(fired), "of", len(batch))  # 2 of 3

    # Wait for TTL to lapse, then prune.
    time.sleep(1.1)
    pruned = deduper.prune()
    print("pruned       ->", pruned)

    # After prune, original opp should fire again.
    print("post-prune   ->", deduper.should_alert(make_opp(net_edge=0.05)))  # True

    print("stats        ->", deduper.stats())
