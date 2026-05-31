#!/usr/bin/env python3
"""
pp_charts_db.py  –  Portfolio Performance charting tool (SQLite/ppxml2db backend)
================================================================
Reads a ppxml2db SQLite database and produces the same
Value / Invested Capital / Delta (P&L) chart that PP shows for
accounts — but scoped to any asset or combination of assets you choose.

Usage
-----
  python pp_charts_db.py portfolio.db                  # interactive picker
  python pp_charts_db.py portfolio.db --list           # list all securities
  python pp_charts_db.py portfolio.db --assets "MSFT" "AAPL"
  python pp_charts_db.py portfolio.db --assets "MSFT" --account "Fidelity"
  python pp_charts_db.py portfolio.db --assets "MSFT" --from 2020-01-01
  python pp_charts_db.py portfolio.db --all            # one chart per security

Requirements:  pip install pandas matplotlib
"""

import sys
import bisect
import argparse
from pathlib import Path
from collections import defaultdict
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.dates as mdates
from datetime import date, timedelta


# ── constants ────────────────────────────────────────────────────────────────

NANO         = 1_000_000_000   # PP stores shares in nano units  (9 decimal places)
HECTO        = 100             # PP stores amounts in hecto units (2 decimal places)
QUOTE_FACTOR = 100_000_000     # PP stores prices/quotes in this unit (8 decimal places)
                               # NOT the same as HECTO — a common source of confusion


# ── XML helpers ──────────────────────────────────────────────────────────────

def _amount(el, tag="amount") -> float:
    """Return float value from a hecto-unit amount element."""
    node = el.find(tag)
    if node is None:
        return 0.0
    return int(node.get("amount", node.text or "0")) / HECTO


def _shares(el) -> float:
    node = el.find("shares")
    if node is None:
        return 0.0
    return int(node.text or "0") / NANO


def _date(el) -> date:
    raw = el.findtext("date", "")[:10]
    return date.fromisoformat(raw) if raw else date.today()


def _text(el, tag) -> str:
    v = el.findtext(tag, "")
    return v.strip() if v else ""


# ── parser ───────────────────────────────────────────────────────────────────

class PPPortfolio:
    """
    Reads a ppxml2db SQLite database. Exposes identical interface to the
    XML-based PPPortfolio so build_series / plot_chart work unchanged.
    All detection and charting methods are identical to pp_charts.py.
    """

    def __init__(self, db_path: str):
        import sqlite3 as _sq
        self.path = db_path
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row

        self.base_currency        = self._load_base_currency(conn)
        self.securities           = {}
        self.accounts             = {}
        self.transactions         = []
        self.account_transactions = []
        self._sec_list            = []   # unused in DB mode but keeps interface intact
        self._parent_map          = {}   # unused in DB mode

        self._load_securities(conn)
        self._load_accounts(conn)
        self._load_transactions(conn)
        conn.close()

        import time as _t
        # Pre-sort prices_raw into parallel arrays ONCE so detection methods
        # can use bisect O(log n) instead of sorted()+min() O(n) per transaction
        _s = _t.time()
        for sec in self.securities.values():
            if sec["prices_raw"]:
                pairs = sorted(sec["prices_raw"].items())
                sec["_raw_dates"]  = [p[0] for p in pairs]
                sec["_raw_values"] = [p[1] for p in pairs]
            else:
                sec["_raw_dates"]  = []
                sec["_raw_values"] = []
        print(f"  pre-sort prices:          {_t.time()-_s:.2f}s")

        # Identical detection pipeline as pp_charts.py
        _s = _t.time(); self._autodetect_price_factor();  print(f"  _autodetect_price_factor: {_t.time()-_s:.2f}s")
        _s = _t.time(); self._apply_price_factor();       print(f"  _apply_price_factor:      {_t.time()-_s:.2f}s")
        _s = _t.time(); self._autodetect_share_factor();  print(f"  _autodetect_share_factor: {_t.time()-_s:.2f}s")

        _s = _t.time()
        for t in self.transactions:
            if t.get("shares_raw", 0) > 0:
                t["shares"] = t["shares_raw"] / self._share_factor
        for t in self.transactions:
            if t["shares"] <= 0 or t["gross"] <= 0:
                continue
            sec = self.securities.get(t["sec_uuid"])
            if sec:
                tx_date = t["date"]
                if tx_date not in sec.get("prices_raw", {}):
                    raw_q = t.get("raw_quote", 0)
                    if raw_q > 0:
                        sec["prices"][tx_date] = raw_q / self._price_factor
                    else:
                        sec["prices"][tx_date] = t["gross"] / t["shares"]
        print(f"  re-apply shares + re-seed:  {_t.time()-_s:.2f}s")

        _s = _t.time(); self._finalise_prices();          print(f"  _finalise_prices:         {_t.time()-_s:.2f}s")
        _s = _t.time(); self._detect_price_mode();        print(f"  _detect_price_mode:       {_t.time()-_s:.2f}s")

    # ── SQLite loaders ────────────────────────────────────────────────────

    @staticmethod
    def _parse_date(s):
        if not s:
            return None
        try:
            from datetime import date as _d
            return _d.fromisoformat(str(s)[:10])
        except (ValueError, TypeError):
            return None

    def _load_base_currency(self, conn) -> str:
        row = conn.execute(
            "SELECT value FROM property WHERE name='baseCurrency'"
        ).fetchone()
        return row["value"] if row else "USD"

    def _load_securities(self, conn):
        for row in conn.execute(
            "SELECT uuid, name, tickerSymbol, currency, isin FROM security"
        ):
            self.securities[row["uuid"]] = {
                "name":       row["name"]         or "",
                "ticker":     row["tickerSymbol"] or "",
                "currency":   row["currency"]     or "",
                "isin":       row["isin"]         or "",
                "prices":     {},
                "prices_raw": {},
            }
        # Load ALL historical prices with native sqlite3 — 0.5s for 350k rows
        for row in conn.execute(
            "SELECT security, tstamp, value FROM price"
        ):
            uuid = row[0]
            if uuid not in self.securities:
                continue
            try:
                d = date.fromisoformat(row[1][:10])
                self.securities[uuid]["prices_raw"][d] = row[2] or 0
            except (ValueError, TypeError):
                pass

    def _load_accounts(self, conn):
        for row in conn.execute("SELECT uuid, name FROM account"):
            if row[0]:
                self.accounts[row[0]] = row[1] or ""

    def _load_transactions(self, conn):
        PORTFOLIO_TYPES = {
            "BUY", "SELL",
            "DELIVERY_INBOUND", "DELIVERY_OUTBOUND",
            "TRANSFER_IN",      "TRANSFER_OUT",
        }
        CASH_TYPES = {
            "FEES", "FEE", "TAXES", "TAX",
            "FEES_REFUND", "TAX_REFUND",
            "DIVIDENDS", "DIVIDEND",
            "INTEREST", "INTEREST_CHARGE",
        }

        # Portfolio transactions — native sqlite3 iteration (fast)
        for row in conn.execute("""
            SELECT x.type, x.date, x.amount, x.shares, x.fees, x.taxes,
                   x.security, a.name
            FROM xact x LEFT JOIN account a ON a.uuid = x.account
            WHERE x.acctype = 'portfolio'
        """):
            tx_type = (row[0] or "").upper()
            if tx_type not in PORTFOLIO_TYPES:
                continue
            sec_uuid = row[6]
            if not sec_uuid or sec_uuid not in self.securities:
                continue
            try:
                tx_date = date.fromisoformat(row[1][:10])
            except (ValueError, TypeError):
                continue
            shares_raw = row[3] or 0
            gross      = (row[2] or 0) / HECTO
            self.transactions.append({
                "date":       tx_date,
                "type":       tx_type,
                "sec_uuid":   sec_uuid,
                "account":    row[7] or "",
                "shares":     shares_raw / NANO,
                "shares_raw": shares_raw,
                "raw_quote":  0,
                "gross":      gross,
                "fees":       (row[4] or 0) / HECTO,
                "taxes":      (row[5] or 0) / HECTO,
            })

        # Cash account transactions — native sqlite3 iteration (fast)
        for row in conn.execute("""
            SELECT x.type, x.date, x.amount, x.security, x.currency, a.name
            FROM xact x LEFT JOIN account a ON a.uuid = x.account
            WHERE x.acctype = 'account'
        """):
            tx_type = (row[0] or "").upper()
            if tx_type not in CASH_TYPES:
                continue
            sec_uuid = row[3]
            if not sec_uuid or sec_uuid not in self.securities:
                continue
            try:
                tx_date = date.fromisoformat(row[1][:10])
            except (ValueError, TypeError):
                continue
            self.account_transactions.append({
                "date":     tx_date,
                "type":     tx_type,
                "sec_uuid": sec_uuid,
                "account":  row[5] or "",
                "amount":   (row[2] or 0) / HECTO,
                "currency": row[4] or "",
            })

    # ── stub: not needed in DB mode ───────────────────────────────────────

    def _resolve_ref(self, *args):
        return ""

    def _build_parent_map(self, client):
        return {}

    # ── fast bisect-based factor detection (overrides XML version methods) ──

    def _autodetect_price_factor(self):
        """Bisect-based factor detection using pre-sorted _raw_dates/_raw_values arrays."""
        CANDIDATES = [100, 10_000, 100_000, 1_000_000, 100_000_000]
        import math
        evidence = []
        # One evidence point per security (not per transaction) — fast and sufficient
        seen_secs = set()
        for t in self.transactions:
            if t["sec_uuid"] in seen_secs:
                continue
            if t["shares_raw"] <= 0 or t["gross"] <= 0:
                continue
            sec = self.securities.get(t["sec_uuid"])
            if not sec or not sec.get("_raw_dates"):
                continue
            raw_dates  = sec["_raw_dates"]
            raw_values = sec["_raw_values"]
            idx = bisect.bisect_right(raw_dates, t["date"]) - 1
            if idx < 0:
                idx = 0
            if idx + 1 < len(raw_dates):
                if abs((raw_dates[idx+1] - t["date"]).days) < abs((raw_dates[idx] - t["date"]).days):
                    idx = idx + 1
            gap = abs((raw_dates[idx] - t["date"]).days)
            if gap > 365:
                continue
            raw_v   = raw_values[idx]
            implied = t["gross"] / (t["shares_raw"] / NANO)
            if implied <= 0 or raw_v <= 0:
                continue
            weight = 1.0 / (1.0 + gap / 30.0)
            evidence.append((raw_v, implied, weight))
            seen_secs.add(t["sec_uuid"])

        if not evidence:
            self._price_factor = QUOTE_FACTOR
            return
        best_factor = CANDIDATES[0]
        best_score  = float("inf")
        total_w     = sum(e[2] for e in evidence)
        for factor in CANDIDATES:
            score = sum(
                w * (math.log(r/factor) - math.log(i)) ** 2
                for r, i, w in evidence if r/factor > 0 and i > 0
            ) / total_w
            if score < best_score:
                best_score, best_factor = score, factor
        self._price_factor = best_factor
    def _autodetect_share_factor(self):
        """Bisect-based: one evidence point per security for speed."""
        CANDIDATES = [100_000_000, 1_000_000_000]
        import math
        evidence = []
        seen_secs = set()
        for t in self.transactions:
            if t["sec_uuid"] in seen_secs:
                continue
            if t["shares_raw"] <= 0 or t["gross"] <= 0:
                continue
            raw_q = t.get("raw_quote", 0)
            if raw_q > 0:
                qd = raw_q / self._price_factor
                if qd > 0:
                    c = t["gross"] / qd
                    if c > 0:
                        evidence.append((t["shares_raw"], c, 2.0))
                        seen_secs.add(t["sec_uuid"])
                        continue
            sec = self.securities.get(t["sec_uuid"])
            if not sec or not sec.get("_raw_dates"):
                continue
            raw_dates  = sec["_raw_dates"]
            raw_values = sec["_raw_values"]
            idx = bisect.bisect_right(raw_dates, t["date"]) - 1
            if idx < 0:
                idx = 0
            gap = abs((raw_dates[idx] - t["date"]).days)
            if gap > 365:
                continue
            pd_ = raw_values[idx] / self._price_factor
            if pd_ <= 0:
                continue
            c = t["gross"] / pd_
            if c <= 0:
                continue
            evidence.append((t["shares_raw"], c, 1.0 / (1.0 + gap / 30.0)))
            seen_secs.add(t["sec_uuid"])

        if not evidence:
            self._share_factor = NANO
            return
        best_factor = CANDIDATES[0]
        best_score  = float("inf")
        total_w     = sum(e[2] for e in evidence)
        for factor in CANDIDATES:
            score = sum(
                w * (math.log(r/factor) - math.log(c)) ** 2
                for r, c, w in evidence if r/factor > 0 and c > 0
            ) / total_w
            if score < best_score:
                best_score, best_factor = score, factor
        self._share_factor = best_factor
    def _apply_price_factor(self):
        """Divide all raw price integers by the detected factor into display prices.
        Raw XML prices always take precedence over tx-seeded quotes."""
        for sec in self.securities.values():
            display = dict(sec["prices"])   # start with tx-seeded prices
            for d, raw_v in sec["prices_raw"].items():
                display[d] = raw_v / self._price_factor  # raw prices OVERWRITE tx-seeded
            sec["prices"] = display

    def _detect_price_mode(self):
        """
        Detect per-security whether historical price entries represent:
          - "per_share": price is per share → value = shares * price  (stocks/ETFs)
          - "total":     price is total holding value → value = price  (real estate etc)

        Method: for each transaction with known gross and shares, find the closest
        price entry and compare:
          error_per_share = |price * shares - gross| / gross
          error_total     = |price           - gross| / gross
        The mode with lower mean error wins.
        Stored in sec["price_mode"] = "per_share" | "total".
        Default is "per_share" when there is insufficient evidence.
        """
        for uuid, sec in self.securities.items():
            sec_txs = [t for t in self.transactions if t["sec_uuid"] == uuid
                       and t["shares"] > 0 and t["gross"] > 0]
            if not sec_txs or not sec["prices_raw"]:
                sec["price_mode"] = "per_share"
                continue

            # Build bisect arrays from RAW xml prices only — NOT the combined
            # prices{} dict which includes tx-seeded quotes (always per-share
            # by construction, so they would bias the detection to per_share).
            raw_pairs   = sorted(sec["prices_raw"].items())
            raw_dates   = [p[0] for p in raw_pairs]
            raw_values  = [p[1] / self._price_factor for p in raw_pairs]
            if not raw_dates:
                sec["price_mode"] = "per_share"
                continue

            err_per_share_list = []
            err_total_list     = []

            for tx in sec_txs:
                idx = bisect.bisect_right(raw_dates, tx["date"]) - 1
                # Use the raw price on or before the transaction date.
                if idx < 0:
                    idx = 0   # no prior price — use earliest available
                p = raw_values[idx]

                gross  = tx["gross"]
                shares = tx["shares"]
                err_per_share_list.append(abs(p * shares - gross) / gross)
                err_total_list.append(    abs(p          - gross) / gross)

            if not err_per_share_list:
                sec["price_mode"] = "per_share"
                continue

            mean_per_share = sum(err_per_share_list) / len(err_per_share_list)
            mean_total     = sum(err_total_list)     / len(err_total_list)

            # Only switch to "total" mode if it's substantially better
            # (factor >2 improvement) to avoid false positives on normal stocks
            if mean_total < mean_per_share / 2.0 and mean_total < 0.5:
                sec["price_mode"] = "total"
            else:
                sec["price_mode"] = "per_share"

    def _finalise_prices(self):
        """
        Convert each security's prices dict to sorted parallel arrays for
        O(log n) bisect lookups instead of O(n log n) linear scans per day.
        Call once after all parsing is complete.
        """
        for sec in self.securities.values():
            raw = sec["prices"]
            if raw:
                pairs = sorted(raw.items())
                sec["price_dates"]  = [p[0] for p in pairs]
                sec["price_values"] = [p[1] for p in pairs]
            else:
                sec["price_dates"]  = []
                sec["price_values"] = []

    def get_price(self, sec_uuid: str, d: date) -> float:
        """
        Return the most recent known price on or before date d (binary search).
        Returns 0.0 if no price history exists.
        When holdings are zero the caller multiplies by 0 so the value is
        always 0 in periods where the position has been fully closed.
        """
        dates  = self.securities[sec_uuid].get("price_dates", [])
        values = self.securities[sec_uuid].get("price_values", [])
        if not dates:
            return 0.0
        idx = bisect.bisect_right(dates, d) - 1
        if idx < 0:
            return values[0]   # before first known price: use earliest
        return values[idx]

    def get_price_raw(self, sec_uuid: str, d: date) -> float:
        """Like get_price but uses ONLY raw XML price entries, ignoring
        tx-seeded quotes.  Used for total-value assets (e.g. real estate)
        where the tx-seeded per-share implied price is meaningless."""
        raw_pairs = sorted(self.securities[sec_uuid].get("prices_raw", {}).items())
        if not raw_pairs:
            return 0.0
        raw_dates  = [p[0] for p in raw_pairs]
        raw_values = [p[1] / self._price_factor for p in raw_pairs]
        idx = bisect.bisect_right(raw_dates, d) - 1
        if idx < 0:
            return raw_values[0]
        return raw_values[idx]

    # ── names ─────────────────────────────────────────────────────────────

    def security_names(self) -> list[str]:
        return sorted(
            s["name"] for s in self.securities.values() if s["name"]
        )

    def uuid_for_name(self, query: str) -> str | None:
        """
        Find a security UUID by name, ticker symbol, or ISIN.
        Matching priority:
          1. Exact ticker (case-insensitive)
          2. Exact ISIN  (case-insensitive)
          3. Exact name  (case-insensitive)
          4. Ticker prefix / substring
          5. Name substring (case-insensitive)
        """
        q = query.strip().lower()

        # 1. Exact ticker
        for uuid, s in self.securities.items():
            if s["ticker"].lower() == q:
                return uuid

        # 2. Exact ISIN
        for uuid, s in self.securities.items():
            if s["isin"].lower() == q:
                return uuid

        # 3. Exact name
        for uuid, s in self.securities.items():
            if s["name"].lower() == q:
                return uuid

        # 4. Ticker prefix or substring
        for uuid, s in self.securities.items():
            if s["ticker"] and q in s["ticker"].lower():
                return uuid

        # 5. Name substring
        for uuid, s in self.securities.items():
            if s["name"] and q in s["name"].lower():
                return uuid

        return None



# ── chart engine ─────────────────────────────────────────────────────────────

def build_series(
    portfolio:    PPPortfolio,
    sec_uuids:    list[str],
    account_name: str | None = None,
    start_date:   date | None = None,
    end_date:     date | None = None,
    debug_dates:  list | None = None,
) -> pd.DataFrame:
    """
    Build a daily time-series DataFrame with columns:
        value           – market value of the selected assets
        invested        – cumulative net invested capital (FIFO cost basis)
        delta           – value − invested  (P&L)

    Parameters
    ----------
    sec_uuids    : list of security UUIDs to include
    account_name : if given, restrict to transactions in that account
    start_date   : chart start (default: first transaction)
    end_date     : chart end   (default: today)
    """
    uuid_set = set(sec_uuids)

    # filter portfolio transactions (buys, sells, deliveries)
    txs = [
        t for t in portfolio.transactions
        if t["sec_uuid"] in uuid_set
        and (account_name is None
             or t["account"].lower() == account_name.lower())
    ]

    # filter cash account transactions (fees, dividends, refunds)
    acct_txs = [
        t for t in portfolio.account_transactions
        if t["sec_uuid"] in uuid_set
        and (account_name is None
             or t["account"].lower() == account_name.lower())
    ]

    all_dates = [t["date"] for t in txs] + [t["date"] for t in acct_txs]
    if not all_dates:
        return pd.DataFrame()

    tx_start = min(all_dates)
    start    = start_date or tx_start
    end      = end_date   or date.today()

    days = pd.date_range(start, end, freq="D")

    # ── invested capital: PP's "Net Deposits" definition ─────────────────
    #
    # PP tracks net cash deployed into a security as:
    #   + BUY / DELIVERY_INBOUND gross amount       (cash out to acquire)
    #   + Fees paid on the security                 (cash out, raises cost)
    #   + Taxes paid on the security                (cash out)
    #   - SELL / DELIVERY_OUTBOUND gross amount     (cash in from disposal)
    #   - Dividends / Interest received             (cash in, lowers net cost)
    #   - Fees Refund / Tax Refund                  (cash in)
    #
    # This is a running cumulative total — it can go negative when cash
    # returned exceeds cash invested (i.e. profitable exit).
    # The market VALUE of the position is tracked separately via prices.

    portfolio_tx_by_date = defaultdict(list)
    for t in txs:
        portfolio_tx_by_date[t["date"]].append(t)

    acct_tx_by_date = defaultdict(list)
    for t in acct_txs:
        acct_tx_by_date[t["date"]].append(t)

    # running state
    holdings   = defaultdict(float)  # uuid -> shares held
    net_invest = defaultdict(float)  # uuid -> cumulative net cash invested

    # types that return cash (reduce net invested)
    CASH_IN_TYPES = {"DIVIDENDS", "DIVIDEND", "INTEREST",
                     "FEES_REFUND", "TAX_REFUND"}
    # types that cost cash (increase net invested)
    CASH_OUT_TYPES = {"FEES", "FEE", "TAXES", "TAX", "INTEREST_CHARGE"}

    rows = []
    for day in days:
        d = day.date()

        # ── portfolio transactions (shares change hands) ──────────────
        # Sort so OUTBOUNDs process before INBOUNDs on the same day,
        # preventing momentary zero-holdings during paired broker transfers.
        day_txs = sorted(portfolio_tx_by_date.get(d, []),
                         key=lambda t: 0 if t["type"] in (
                             "SELL", "DELIVERY_OUTBOUND", "TRANSFER_OUT") else 1)
        for t in day_txs:
            typ = t["type"]
            g   = t["gross"]
            if debug_dates and d in debug_dates:
                print(f"  TX {d}: {typ} shares={t['shares']:.4f} gross={g:.2f} "
                      f"acct={t['account']} net_invest_before={net_invest[t['sec_uuid']]:.2f}")
            if typ == "BUY":
                # Real cash outflow — increases invested capital
                holdings[t["sec_uuid"]]   += t["shares"]
                net_invest[t["sec_uuid"]] += g
            elif typ == "SELL":
                # Real cash inflow — decreases invested capital
                holdings[t["sec_uuid"]]    = max(
                    holdings[t["sec_uuid"]] - t["shares"], 0.0)
                net_invest[t["sec_uuid"]] -= g
            elif typ in ("DELIVERY_INBOUND", "TRANSFER_IN"):
                # Shares arrive — treat like a BUY for invested capital.
                # User records actual purchases as deliveries when not tracking cash.
                # Broker-to-broker transfer pairs net to zero automatically.
                holdings[t["sec_uuid"]]   += t["shares"]
                net_invest[t["sec_uuid"]] += g
            elif typ in ("DELIVERY_OUTBOUND", "TRANSFER_OUT"):
                # Shares leave — treat like a SELL for invested capital.
                holdings[t["sec_uuid"]] = max(
                    holdings[t["sec_uuid"]] - t["shares"], 0.0)
                net_invest[t["sec_uuid"]] -= g

        # ── cash account transactions (no share movement) ─────────────
        for t in acct_tx_by_date.get(d, []):
            typ = t["type"]
            amt = t["amount"]
            # Include all cash account transactions regardless of currency.
            # PP stores amounts in the account's currency; for multi-currency
            # portfolios this means some amounts may need FX conversion for
            # perfect accuracy, but including them as-is is better than omitting.
            if typ in CASH_IN_TYPES:
                net_invest[t["sec_uuid"]] -= amt  # money received → cost basis down
            elif typ in CASH_OUT_TYPES:
                net_invest[t["sec_uuid"]] += amt  # money paid → cost basis up

        # market value: per-share assets → shares × price
        #               total-value assets  → raw xml price directly (e.g. real estate)
        mkt = 0.0
        for u in uuid_set:
            mode = portfolio.securities[u].get("price_mode", "per_share")
            if mode == "total":
                mkt += portfolio.get_price_raw(u, d) if holdings[u] > 0 else 0.0
            else:
                price = portfolio.get_price(u, d)
                sh    = holdings[u]
                mkt  += sh * price
                if debug_dates and d in debug_dates:
                    print(f"  DEBUG {d} [{u[:8]}]: holdings={sh:.6f}  "
                          f"price={price:.4f}  contrib={sh*price:.2f}  "
                          f"net_invest={net_invest[u]:.2f}")
        inv = sum(net_invest[u] for u in uuid_set)
        if debug_dates and day.date() in debug_dates:
            print(f"  DEBUG {day.date()} TOTAL: mkt={mkt:.2f}  inv={inv:.2f}  delta={mkt-inv:.2f}")
        rows.append({"date": day, "value": mkt, "invested": inv, "delta": mkt - inv})

    df = pd.DataFrame(rows).set_index("date")
    return df


def plot_chart(
    df:         pd.DataFrame,
    title:      str,
    currency:   str = "USD",
    save_path:  str | None = None,
):
    """Render the value / invested capital / delta chart."""
    if df.empty:
        print(f"  [!] No data to plot for: {title}")
        return

    fig, ax = plt.subplots(figsize=(13, 6))
    fig.patch.set_facecolor("#f8f9fa")
    ax.set_facecolor("#f8f9fa")

    # filled delta area (green above zero, red below)
    ax.fill_between(df.index, df["delta"], 0,
                    where=df["delta"] >= 0,
                    alpha=0.25, color="#2ecc71", label="_nolegend_")
    ax.fill_between(df.index, df["delta"], 0,
                    where=df["delta"] < 0,
                    alpha=0.25, color="#e74c3c", label="_nolegend_")

    # three main lines
    ax.plot(df.index, df["value"],    color="#9b59b6", lw=1.8,
            label="Value")
    ax.plot(df.index, df["invested"], color="#f39c12", lw=1.5,
            label="Invested Capital")
    ax.plot(df.index, df["delta"],    color="#2980b9", lw=1.5,
            label="Delta (P&L)")

    # zero baseline
    ax.axhline(0, color="#aaaaaa", lw=0.8, ls="--")

    # formatting
    fmt = mticker.FuncFormatter(
        lambda v, _: f"{v:,.0f} {currency}" if abs(v) < 1e6
        else f"{v/1e6:,.2f}M {currency}"
    )
    ax.yaxis.set_major_formatter(fmt)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[4, 7, 10]))

    ax.grid(axis="y", color="#dddddd", lw=0.6)
    ax.grid(axis="x", color="#eeeeee", lw=0.4)
    ax.spines[["top", "right"]].set_visible(False)

    # final annotation
    last = df.iloc[-1]
    pct  = (last["delta"] / last["invested"] * 100) if last["invested"] else 0
    sign = "+" if last["delta"] >= 0 else ""
    ax.annotate(
        f"P&L: {sign}{last['delta']:,.0f} {currency} ({sign}{pct:.1f}%)",
        xy=(df.index[-1], last["delta"]),
        xytext=(-120, 12),
        textcoords="offset points",
        fontsize=8.5,
        color="#2980b9",
        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#2980b9", alpha=0.8),
    )

    ax.set_title(title, fontsize=13, fontweight="bold", pad=12)
    ax.legend(loc="upper left", framealpha=0.9, fontsize=9)
    fig.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"  ✓ Saved: {save_path}")
    else:
        plt.show()
    plt.close(fig)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Portfolio Performance per-asset charting tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("xml_file", help="Path to your ppxml2db SQLite .db file")
    p.add_argument("--list",    action="store_true",
                   help="List all securities in the file and exit")
    p.add_argument("--assets",  nargs="+", metavar="NAME",
                   help="Security name(s) to chart (partial match OK). "
                        "Multiple names are combined into one chart.")
    p.add_argument("--account", metavar="NAME",
                   help="Restrict to a specific securities account")
    p.add_argument("--from",   dest="date_from", metavar="YYYY-MM-DD",
                   help="Chart start date")
    p.add_argument("--to",     dest="date_to",   metavar="YYYY-MM-DD",
                   help="Chart end date (default: today)")
    p.add_argument("--all",    action="store_true",
                   help="Generate one chart per security and save PNGs")
    p.add_argument("--save",   metavar="FILE",
                   help="Save chart to PNG instead of displaying")
    p.add_argument("--debug-prices", dest="debug_prices", action="store_true",
                   help="Print raw price values and detected factor for each security, then exit")
    p.add_argument("--dump-xml-prices", dest="dump_xml_prices", metavar="NAME",
                   help="Dump raw XML price v-values for a named security before any conversion, then exit")
    p.add_argument("--dump-raw-tx", dest="dump_raw_tx", metavar="NAME",
                   help="Dump raw XML of first 5 transactions for a security (diagnose reference issues)")
    p.add_argument("--list-tx", dest="list_tx", metavar="NAME",
                   help="List all parsed transactions for a security in date order (diagnose wrong values)")
    p.add_argument("--list-accounts", action="store_true",
                   help="List all accounts and portfolios found in the XML, with transaction counts")
    p.add_argument("--dump-ancestors", dest="dump_ancestors", metavar="TX_UUID",
                   help="Show full XML ancestor chain for a transaction UUID (diagnose wrong account names)")
    return p.parse_args()


def main():
    args = parse_args()

    xml_path = Path(args.xml_file)
    if not xml_path.exists():
        print(f"Error: file not found: {xml_path}")
        sys.exit(1)

    import time as _time; _t0 = _time.time()
    print(f"Reading: {xml_path.name} …")
    pp = PPPortfolio(str(xml_path))
    print(f"  Loaded in {_time.time()-_t0:.1f}s")
    print(f"  {len(pp.securities)} securities, "
          f"{len(pp.transactions)} portfolio transactions, "
          f"{len(pp.account_transactions)} cash account transactions found.")
    print(f"  Price factor auto-detected: {pp._price_factor:,}  |  Share factor auto-detected: {pp._share_factor:,}")

    if args.dump_ancestors:
        target_uuid = args.dump_ancestors.strip()
        tree2 = ET.parse(xml_path)
        root2 = tree2.getroot()
        client2 = root2 if root2.tag == "client" else root2.find("client") or root2
        # Build full parent map
        parent_map = {c: p for p in client2.iter() for c in p}
        found = False
        for el in client2.iter():
            if el.findtext("uuid", "").strip() == target_uuid:
                found = True
                print(f"\nFound element <{el.tag}> with UUID {target_uuid}")
                print("\nAncestor chain (bottom to top):")
                cur = el
                depth = 0
                while cur in parent_map:
                    cur = parent_map[cur]
                    name = cur.findtext("name", "")
                    uuid = cur.findtext("uuid", "")
                    print(f"  {'  '*depth}<{cur.tag}>"
                          f"  name={name!r}"
                          f"  uuid={uuid[:12] if uuid else ''}")
                    depth += 1
                    if depth > 15:
                        break
                break
        if not found:
            print(f"UUID {target_uuid} not found in XML")
            # Show first few portfolio-transaction UUIDs as examples
            print("\nFirst 5 portfolio-transaction UUIDs in XML:")
            for i, tx in enumerate(client2.iter("portfolio-transaction")):
                u = tx.findtext("uuid","")
                d = tx.findtext("date","")[:10]
                t = tx.findtext("type","")
                print(f"  {u}  {d}  {t}")
                if i >= 4: break
        return

    if args.list_accounts:
        print(f"\n{'Account/Portfolio':<40} {'UUID':<38} {'Tx count'}")
        print("-" * 85)
        for uuid, name in sorted(pp.accounts.items(), key=lambda x: x[1]):
            tx_count = sum(1 for t in pp.transactions if t["account"] == name)
            print(f"  {name:<38} {uuid:<38} {tx_count}")
        return

    if args.list_tx:
        uuid = pp.uuid_for_name(args.list_tx)
        if not uuid:
            print(f"Security not found: {args.list_tx}")
            sys.exit(1)
        sec = pp.securities[uuid]
        print(f"\nPortfolio transactions for: {sec['name']}")
        print(f"{'Date':<12} {'Type':<22} {'Shares':>12} {'Gross':>14} {'Account'}")
        print("-" * 80)
        ptxs = sorted([t for t in pp.transactions if t['sec_uuid'] == uuid],
                      key=lambda t: t['date'])
        for t in ptxs:
            print(f"  {t['date']}  {t['type']:<20}  {t['shares']:>12.4f}  {t['gross']:>14,.2f}  {t['account']}")
        print(f"\nCash account transactions for: {sec['name']}")
        print(f"{'Date':<12} {'Type':<22} {'Amount':>14} {'Account'}")
        print("-" * 60)
        atxs = sorted([t for t in pp.account_transactions if t['sec_uuid'] == uuid],
                      key=lambda t: t['date'])
        for t in atxs:
            print(f"  {t['date']}  {t['type']:<20}  {t['amount']:>14,.2f}  {t['account']}")
        print(f"\nTotal: {len(ptxs)} portfolio tx, {len(atxs)} account tx")
        return

    if args.dump_raw_tx:
        query = args.dump_raw_tx
        uuid  = pp.uuid_for_name(query)
        target_name = pp.securities[uuid]["name"] if uuid else query
        print(f"\nSearching RAW XML for: {target_name}  (UUID: {uuid})")

        tree2 = ET.parse(xml_path)
        root2 = tree2.getroot()
        client2 = root2 if root2.tag == "client" else root2.find("client") or root2

        # Search ALL portfolio-transaction elements anywhere in the document
        # PP uses both kebab-case <portfolio-transaction> (Version A: portfolio primary)
        # and camelCase <portfolioTransaction> (Version B: account primary, inline child)
        found = 0
        all_ptxs = list(client2.iter("portfolio-transaction")) +                    list(client2.iter("portfolioTransaction"))
        for tx in all_ptxs:
            sec_el = tx.find("security")
            if sec_el is None:
                continue
            inline_uuid = sec_el.findtext("uuid", "")
            ref_attr    = sec_el.get("reference", "")
            uuid_match  = bool(uuid and inline_uuid == uuid)
            ref_match   = bool(uuid and ref_attr and
                               pp._resolve_ref(client2, ref_attr, tx) == uuid)
            if uuid_match or ref_match:
                raw   = ET.tostring(tx, encoding="unicode")
                import re as _re
                pretty = _re.sub(r">\s*<", ">\n    <", raw[:1000])
                print(f"\n  [Match {found+1}] uuid_match={uuid_match} ref_match={ref_match}")
                print(f"  ref_attr='{ref_attr}'  inline_uuid='{inline_uuid}'")
                print("  " + pretty)
                found += 1
                if found >= 5:
                    break

        print(f"\nTotal matches in raw XML: {found}")
        if found == 0:
            # Check if UUID appears anywhere at all
            raw_full = ET.tostring(client2, encoding="unicode")
            if uuid and uuid in raw_full:
                import re as _re
                occ = list(_re.finditer(_re.escape(uuid), raw_full))
                print(f"UUID appears {len(occ)} times in XML but none in portfolio-transaction/security")
                for i, m in enumerate(occ[:4]):
                    ctx = raw_full[max(0,m.start()-300):m.end()+300]
                    print(f"\n  Occurrence {i+1}:")
                    print("  " + _re.sub(r">\s*<", ">\n  <", ctx))
            else:
                print("UUID not found anywhere in raw XML.")
        return


    if args.dump_xml_prices:
        name = args.dump_xml_prices
        uuid = pp.uuid_for_name(name)
        if not uuid:
            print(f"Security not found: {name}")
            sys.exit(1)
        sec = pp.securities[uuid]
        print(f"\nSecurity: {sec['name']}")
        print(f"Detected price factor: {pp._price_factor:,}")
        print(f"\nRaw <prices> entries from XML (first 20):")
        raw_items = sorted(sec["prices_raw"].items())[:20]
        if not raw_items:
            print("  (none)")
        for d, v in raw_items:
            print(f"  {d}  v={v:>25,}  ÷{pp._price_factor:,} = {v/pp._price_factor:,.6f}")
        print(f"\nTransaction quotes (seeded into prices dict):")
        tx_seeded = {d: v for d, v in sec["prices"].items() if d not in sec["prices_raw"]}
        for d, v in sorted(tx_seeded.items()):
            print(f"  {d}  display = {v:,.6f}  (from transaction quote)")
        print(f"\nTransactions for this security:")
        for t in sorted([t for t in pp.transactions if t['sec_uuid'] == uuid], key=lambda t: t['date']):
            print(f"  {t['date']}  {t['type']:20s}  shares={t['shares']:.6f}  gross={t['gross']:,.2f}  implied_price={t['gross']/t['shares'] if t['shares'] else 0:,.2f}")
        return

    if args.debug_prices:
        print(f"\nPrice factor: {pp._price_factor:,}\n")
        for uuid, sec in sorted(pp.securities.items(), key=lambda x: x[1]["name"]):
            if not sec["prices_raw"] and not sec["prices"]:
                continue
            print(f"  {sec['name']} ({sec['ticker'] or sec['isin'] or uuid[:8]})")
            # show raw entries (from XML <prices>)
            for d, raw in sorted(sec["prices_raw"].items()):
                display = raw / pp._price_factor
                print(f"    {d}  raw={raw:>18,}  →  {display:>14,.4f} {sec['currency']}")
            print(f"    price_mode: {sec.get('price_mode', 'unknown')}")
            # show tx-seeded prices (from transaction quotes, not in prices_raw)
            tx_seeded = {d: v for d, v in sec["prices"].items()
                         if d not in sec["prices_raw"]}
            for d, v in sorted(tx_seeded.items()):
                print(f"    {d}  [from tx quote]  →  {v:>14,.4f} {sec['currency']}")
            print()
        return

    if args.list:
        print(f"\n{'Name':<55} {'Ticker':<10} {'ISIN'}")
        print("-" * 85)
        rows = sorted(
            [(s["name"], s["ticker"], s["isin"])
             for s in pp.securities.values() if s["name"]],
            key=lambda r: r[0].lower()
        )
        for name, ticker, isin in rows:
            print(f"  {name:<53} {ticker:<10} {isin}")
        return

    start = date.fromisoformat(args.date_from) if args.date_from else None
    end   = date.fromisoformat(args.date_to)   if args.date_to   else None

    # ── --all: one chart per security ────────────────────────────────────
    if args.all:
        out_dir = xml_path.parent / "pp_charts"
        out_dir.mkdir(exist_ok=True)
        for uuid, sec in pp.securities.items():
            if not sec["name"]:
                continue
            print(f"  Charting: {sec['name']}")
            df = build_series(pp, [uuid], args.account, start, end)
            safe = "".join(c if c.isalnum() else "_" for c in sec["name"])
            plot_chart(
                df,
                title=f"{sec['name']} – Value / Invested Capital / Delta",
                currency=sec["currency"] or pp.base_currency,
                save_path=str(out_dir / f"{safe}.png"),
            )
        print(f"\nDone. Charts saved to: {out_dir}/")
        return

    # ── specific assets (or interactive picker) ───────────────────────────
    if args.assets:
        chosen_names = args.assets
    else:
        # interactive: print numbered list and let user pick
        names = pp.security_names()
        if not names:
            print("No securities found in file.")
            sys.exit(1)
        print("\nAvailable securities:")
        for i, n in enumerate(names, 1):
            print(f"  {i:3}. {n}")
        raw = input(
            "\nEnter number(s) to chart (e.g. 1  or  2,5,7  or  1-3): "
        ).strip()
        chosen_names = []
        for part in raw.replace(",", " ").split():
            if "-" in part:
                lo, hi = part.split("-", 1)
                chosen_names.extend(names[int(lo)-1 : int(hi)])
            else:
                try:
                    chosen_names.append(names[int(part)-1])
                except (ValueError, IndexError):
                    pass

    # resolve to UUIDs
    uuids = []
    for name in chosen_names:
        uuid = pp.uuid_for_name(name)
        if uuid:
            uuids.append(uuid)
        else:
            print(f"  [!] Security not found: '{name}' — skipped.")

    if not uuids:
        print("No matching securities found. Use --list to see available names.")
        sys.exit(1)

    label_names = [pp.securities[u]["name"] for u in uuids]
    title_assets = " + ".join(label_names)
    acct_suffix  = f"  [{args.account}]" if args.account else ""
    title = f"{title_assets}{acct_suffix} – Value / Invested Capital / Delta"

    currency = pp.securities[uuids[0]]["currency"] or pp.base_currency

    print(f"\nBuilding chart for: {title_assets}")
    df = build_series(pp, uuids, args.account, start, end)

    if df.empty:
        print("No transactions found for the selected securities/filters.")
        sys.exit(1)

    print(f"  Date range: {df.index[0].date()} → {df.index[-1].date()}")
    last = df.iloc[-1]
    pct  = (last['delta'] / last['invested'] * 100) if last['invested'] else 0
    print(f"  Final value:     {last['value']:>12,.2f} {currency}")
    print(f"  Invested capital:{last['invested']:>12,.2f} {currency}")
    print(f"  Delta (P&L):     {last['delta']:>12,.2f} {currency}  ({pct:+.1f}%)")

    plot_chart(df, title=title, currency=currency, save_path=args.save)


if __name__ == "__main__":
    main()
