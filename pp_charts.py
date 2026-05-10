#!/usr/bin/env python3
"""
pp_charts.py  –  Portfolio Performance asset-level charting tool
================================================================
Reads a Portfolio Performance XML file and produces the same
Value / Invested Capital / Delta (P&L) chart that PP shows for
accounts — but scoped to any asset or combination of assets you choose.

Usage
-----
  python pp_charts.py portfolio.xml                  # interactive picker
  python pp_charts.py portfolio.xml --list           # list all securities
  python pp_charts.py portfolio.xml --assets "MSFT" "AAPL"
  python pp_charts.py portfolio.xml --assets "MSFT" --account "Fidelity"
  python pp_charts.py portfolio.xml --assets "MSFT" --from 2020-01-01
  python pp_charts.py portfolio.xml --all            # one chart per security

Requirements:  pip install pandas matplotlib lxml
"""

import sys
import bisect
import argparse
from pathlib import Path
from collections import defaultdict
import xml.etree.ElementTree as ET
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
    """Parses a Portfolio Performance XML file into usable Python structures."""

    def __init__(self, xml_path: str):
        self.path = xml_path
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # PP wraps everything in <client>
        client = root if root.tag == "client" else root.find("client") or root

        self.base_currency = client.findtext("baseCurrency", "USD")
        self.securities    = {}   # uuid -> dict
        self.accounts      = {}   # uuid -> name
        self.transactions  = []   # list of dicts  (portfolio: BUY/SELL/DELIVERY)
        self.account_transactions = []  # list of dicts (cash: fees/dividends/etc)

        # Cache the top-level securities list for O(1) reference resolution.
        # PP XML references use 1-based indices into this exact list:
        #   reference="../../securities/security[42]"
        # Must use direct path (not recursive .//), matching PP's XPath semantics.
        self._sec_list = client.findall("securities/security")

        self._parse_securities(client)
        self._parse_accounts(client)
        self._parse_transactions(client)       # also seeds prices from tx quotes
        self._parse_account_transactions(client)
        self._autodetect_price_factor()        # figure out the raw v scale
        self._finalise_prices()                # sort price arrays for fast bisect lookup
        self._detect_price_mode()              # per-share vs total-value prices

    # ── securities ────────────────────────────────────────────────────────

    def _parse_securities(self, client):
        for sec in client.findall(".//securities/security"):
            uuid  = _text(sec, "uuid")
            name  = _text(sec, "name")
            if not uuid:
                continue
            prices_raw = {}   # date -> raw integer v (factor unknown until auto-detect)
            for p in sec.findall(".//prices/price"):
                d = p.get("t", "")[:10]
                v = p.get("v", "0")
                try:
                    prices_raw[date.fromisoformat(d)] = int(v)
                except Exception:
                    pass
            prices = {}   # will be populated (divided by factor) after auto-detect
            self.securities[uuid] = {
                "name":      name,
                "ticker":    _text(sec, "tickerSymbol"),
                "currency":  _text(sec, "currencyCode"),
                "isin":      _text(sec, "isin"),
                "prices":    prices,       # populated after auto-detect
                "prices_raw": prices_raw,  # raw integer v values
            }

    # ── accounts ──────────────────────────────────────────────────────────

    def _parse_accounts(self, client):
        # deposit / cash accounts
        for acc in client.findall(".//accounts/account"):
            uuid = _text(acc, "uuid")
            name = _text(acc, "name")
            if uuid:
                self.accounts[uuid] = name
        # securities / portfolio accounts — may be defined under <portfolios>
        # OR inline inside <crossEntry> elements within account transactions
        for acc in client.findall(".//portfolio"):
            uuid = _text(acc, "uuid")
            name = _text(acc, "name")
            if uuid and uuid not in self.accounts:
                self.accounts[uuid] = name

    def _parse_account_transactions(self, client):
        """
        Parse cash account transactions that affect net invested capital
        for a security: Fees, Taxes, Dividends, Interest, FeesRefund, TaxRefund.

        These live in <accounts><account><transactions><account-transaction>
        and reference a security UUID when they relate to a specific holding.
        """
        for account in client.findall(".//accounts/account"):
            acct_name = _text(account, "name")
            for tx in account.findall(".//transactions/account-transaction"):
                tx_type = _text(tx, "type").upper()
                # Only types that adjust invested capital for a security
                if tx_type not in ("FEES", "TAXES", "DIVIDENDS", "INTEREST",
                                   "FEES_REFUND", "TAX_REFUND",
                                   # PP uses these spellings too:
                                   "FEE", "TAX", "DIVIDEND",
                                   "INTEREST_CHARGE"):
                    continue

                # Must reference a security to be relevant
                sec_el   = tx.find("security")
                sec_uuid = None
                if sec_el is not None:
                    sec_uuid = _text(sec_el, "uuid")
                    if not sec_uuid:
                        ref = sec_el.get("reference", "")
                        if ref:
                            sec_uuid = self._resolve_ref(client, ref, account)

                if not sec_uuid or sec_uuid not in self.securities:
                    continue

                amount_el = tx.find("amount")
                amount = int(amount_el.text or "0") / HECTO if amount_el is not None else 0.0

                self.account_transactions.append({
                    "date":     _date(tx),
                    "type":     tx_type,
                    "sec_uuid": sec_uuid,
                    "account":  acct_name,
                    "amount":   amount,
                })

    # ── transactions ──────────────────────────────────────────────────────

    def _parse_transactions(self, client):
        """
        Collect all <portfolio-transaction> elements from the document.

        PP uses XStream serialization which stores BUY/SELL as cross-entries:
        the <portfolio> containing the shares leg is defined INLINE inside a
        <crossEntry> element within the cash account-transaction, not under
        the top-level <portfolios> element.  We therefore search the entire
        document for portfolio-transaction elements rather than walking only
        <portfolios>.

        Structure for BUY/SELL:
          <accounts><account><transactions><account-transaction>
            <crossEntry class="buysell">
              <portfolio>                        ← defined here, not in <portfolios>
                <transactions>
                  <portfolio-transaction>...</>  ← shares leg we want
                </transactions>
              </portfolio>
            </crossEntry>
          </account-transaction>

        Structure for DELIVERY / TRANSFER:
          <portfolios><portfolio><transactions>
            <portfolio-transaction>...</>        ← standalone, no cross-entry
          </portfolios>
        """
        for portfolio in client.findall(".//portfolio"):
            port_uuid = _text(portfolio, "uuid")
            port_name = self.accounts.get(port_uuid, _text(portfolio, "name"))

            for tx in portfolio.findall("transactions/portfolio-transaction"):
                tx_type = _text(tx, "type").upper()
                if tx_type not in ("BUY", "SELL",
                                   "TRANSFER_IN",  "TRANSFER_OUT",
                                   "DELIVERY_INBOUND", "DELIVERY_OUTBOUND"):
                    continue

                # resolve security UUID (PP uses XML references)
                sec_el   = tx.find("security")
                sec_uuid = None
                if sec_el is not None:
                    # direct uuid child
                    sec_uuid = _text(sec_el, "uuid")
                    if not sec_uuid:
                        # reference attribute: ../../../securities/security[n]
                        ref = sec_el.get("reference", "")
                        if ref:
                            # resolve relative XPath reference by scanning
                            sec_uuid = self._resolve_ref(client, ref, portfolio)

                if not sec_uuid or sec_uuid not in self.securities:
                    continue

                shares = _shares(tx)
                tx_date = _date(tx)

                # gross amount is stored in the amount element (hecto)
                amount_el = tx.find("amount")
                gross = int(amount_el.text or "0") / HECTO if amount_el is not None else 0.0

                # fees + taxes
                fees  = sum(_amount(u, "amount") for u in tx.findall("units/unit[@type='FEE']"))
                taxes = sum(_amount(u, "amount") for u in tx.findall("units/unit[@type='TAX']"))

                # ── seed price from transaction quote ──────────────────────
                # PP stores a per-share quote on every transaction in a
                # <quote> element using QUOTE_FACTOR units (×100_000_000).
                # For assets with no price feed (e.g. real estate), this is
                # often the only price data available, so we inject it into
                # the security's prices dict.  Manual <prices> entries
                # (already parsed) take precedence if they exist on same date
                # because they're loaded first and we don't overwrite here.
                quote_el = tx.find("quote")
                if quote_el is not None and quote_el.text:
                    try:
                        q = int(quote_el.text) / QUOTE_FACTOR
                        if q > 0 and tx_date not in self.securities[sec_uuid]["prices"]:
                            self.securities[sec_uuid]["prices"][tx_date] = q
                    except (ValueError, TypeError):
                        pass
                elif shares > 0 and gross > 0:
                    # Fallback: derive price from amount ÷ shares when no
                    # explicit quote element (both already in display units)
                    q = gross / shares
                    if q > 0 and tx_date not in self.securities[sec_uuid]["prices"]:
                        self.securities[sec_uuid]["prices"][tx_date] = q

                self.transactions.append({
                    "date":      tx_date,
                    "type":      tx_type,
                    "sec_uuid":  sec_uuid,
                    "account":   port_name,
                    "shares":    shares,
                    "gross":     gross,
                    "fees":      fees,
                    "taxes":     taxes,
                })

    def _resolve_ref(self, client, ref: str, context_el) -> str:
        """
        Resolve a PP XPath reference such as:
            ../../../../../../securities/security[42]

        PP uses 1-based indexing into the top-level <securities> list.
        We use self._sec_list which is built from the direct child path
        "securities/security" (not recursive) to match PP's semantics exactly.
        """
        import re
        m = re.search(r"securities/security(?:\[(\d+)\])?$", ref)
        if m:
            idx = int(m.group(1) or "1") - 1
            if 0 <= idx < len(self._sec_list):
                return _text(self._sec_list[idx], "uuid")
        return ""

    # ── price lookup ──────────────────────────────────────────────────────

    def _autodetect_price_factor(self):
        """
        PP's XML stores historical prices as raw integers whose scale factor
        is not written in the file.  Different PP versions and data sources
        have used different factors (100, 100_000, 100_000_000 are all seen
        in the wild).  Rather than hardcoding a guess, we derive the factor
        from the data itself:

        For each security that has BOTH raw price entries AND at least one
        portfolio transaction with a known per-share price (gross / shares),
        we compute the implied display price from the transaction and find
        the factor that makes the nearest raw price entry match it.

        Candidate factors tried: 100, 10_000, 100_000, 1_000_000, 100_000_000.
        We pick the factor whose implied prices are closest (in log scale) to
        the transaction-derived prices across all evidence we can find.

        The winning factor is then applied globally: all securities in one
        PP file use the same factor.  Transaction-seeded prices (which are
        already in display units) are kept as-is with a sentinel factor=1.
        """
        CANDIDATES = [100, 10_000, 100_000, 1_000_000, 100_000_000]

        # Gather evidence: (raw_v, implied_display_price) pairs
        #
        # Strategy A (strongest): use the transaction quote itself as the anchor.
        # Each portfolio transaction carries a per-share quote in QUOTE_FACTOR units
        # stored in prices_raw under the transaction date.  Since we seeded that
        # into sec["prices"] (display units) but NOT into sec["prices_raw"], we can
        # pair them directly: raw_v from prices_raw on tx date vs display quote
        # from sec["prices"] on the same date.  Zero-day gap = perfect anchor.
        #
        # Strategy B (fallback): find the closest raw price entry to any transaction
        # and use the transaction-implied price (gross/shares) as reference.
        # No distance limit — we just use the globally closest entry, weighted by
        # how far it is (closer = more weight).
        evidence = []   # (raw_v, implied_display_price, weight)
        for uuid, sec in self.securities.items():
            if not sec["prices_raw"]:
                continue

            # Strategy A: tx-date quotes seeded into sec["prices"] but not prices_raw
            sec_txs = [t for t in self.transactions if t["sec_uuid"] == uuid
                       and t["shares"] > 0 and t["gross"] > 0]
            for tx in sec_txs:
                tx_date = tx["date"]
                # quote in display units (seeded by _parse_transactions)
                display_quote = sec["prices"].get(tx_date)
                # same date in raw (only present if a manual price entry lands on tx date)
                raw_v = sec["prices_raw"].get(tx_date)
                if display_quote and raw_v:
                    evidence.append((raw_v, display_quote, 10.0))  # high weight

            # Strategy B: for each raw price entry, find the nearest transaction
            raw_dates = sorted(sec["prices_raw"].keys())
            for raw_date, raw_v in sec["prices_raw"].items():
                if not sec_txs:
                    continue
                # find closest transaction by date
                closest_tx = min(sec_txs, key=lambda t: abs((t["date"] - raw_date).days))
                gap_days = abs((closest_tx["date"] - raw_date).days)
                implied = closest_tx["gross"] / closest_tx["shares"]
                # weight decays with distance; even 2-year gap still contributes weakly
                weight = 1.0 / (1.0 + gap_days / 30.0)
                evidence.append((raw_v, implied, weight))

        if not evidence:
            # No cross-reference possible — fall back to QUOTE_FACTOR default
            # but also try to infer from magnitude of raw values
            all_raw = []
            for sec in self.securities.values():
                all_raw.extend(sec["prices_raw"].values())
            if all_raw:
                median_raw = sorted(all_raw)[len(all_raw) // 2]
                # A "reasonable" price is between 0.01 and 1_000_000
                # Pick the factor that puts the median in that range
                for factor in CANDIDATES:
                    implied = median_raw / factor
                    if 0.001 <= implied <= 10_000_000:
                        self._price_factor = factor
                        break
                else:
                    self._price_factor = QUOTE_FACTOR
            else:
                self._price_factor = QUOTE_FACTOR
            self._apply_price_factor()
            return

        import math
        best_factor = CANDIDATES[0]
        best_score  = float("inf")
        total_weight = sum(w for _, _, w in evidence)
        for factor in CANDIDATES:
            # weighted mean squared log-ratio; 0 = perfect match
            score = 0.0
            for raw_v, implied, weight in evidence:
                candidate_price = raw_v / factor
                if candidate_price <= 0 or implied <= 0:
                    score += weight * 1e9
                    continue
                score += weight * (math.log(candidate_price) - math.log(implied)) ** 2
            score /= total_weight
            if score < best_score:
                best_score  = score
                best_factor = factor

        self._price_factor = best_factor
        self._apply_price_factor()

    def _apply_price_factor(self):
        """Divide all raw price integers by the detected factor into display prices."""
        for sec in self.securities.values():
            # prices already seeded from transaction quotes are in display units
            # (added with factor=1 in _parse_transactions); keep those.
            # Only convert the raw XML price entries.
            display = dict(sec["prices"])   # copy of tx-seeded prices
            for d, raw_v in sec["prices_raw"].items():
                if d not in display:        # don't overwrite tx-seeded prices
                    display[d] = raw_v / self._price_factor
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
        for t in portfolio_tx_by_date.get(d, []):
            typ = t["type"]
            g   = t["gross"]
            if typ in ("BUY", "TRANSFER_IN", "DELIVERY_INBOUND"):
                holdings[t["sec_uuid"]]   += t["shares"]
                net_invest[t["sec_uuid"]] += g   # cash out → invested goes up
            elif typ in ("SELL", "TRANSFER_OUT", "DELIVERY_OUTBOUND"):
                holdings[t["sec_uuid"]]    = max(
                    holdings[t["sec_uuid"]] - t["shares"], 0.0
                )
                net_invest[t["sec_uuid"]] -= g   # cash in → invested goes down

        # ── cash account transactions (no share movement) ─────────────
        for t in acct_tx_by_date.get(d, []):
            typ = t["type"]
            amt = t["amount"]
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
                # price entry IS the total holding value; use raw prices only
                # (tx-seeded quotes are per-share implied prices, wrong for total mode)
                mkt += portfolio.get_price_raw(u, d) if holdings[u] > 0 else 0.0
            else:
                mkt += holdings[u] * portfolio.get_price(u, d)
        inv = sum(net_invest[u] for u in uuid_set)
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
    p.add_argument("xml_file", help="Path to your .xml Portfolio Performance file")
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
    return p.parse_args()


def main():
    args = parse_args()

    xml_path = Path(args.xml_file)
    if not xml_path.exists():
        print(f"Error: file not found: {xml_path}")
        sys.exit(1)

    print(f"Reading: {xml_path.name} …")
    pp = PPPortfolio(str(xml_path))
    print(f"  {len(pp.securities)} securities, "
          f"{len(pp.transactions)} portfolio transactions, "
          f"{len(pp.account_transactions)} cash account transactions found.")
    print(f"  Price factor auto-detected: {pp._price_factor:,}")

    if args.dump_raw_tx:
        import xml.etree.ElementTree as ET2
        query  = args.dump_raw_tx
        uuid   = pp.uuid_for_name(query)
        target_name = pp.securities[uuid]["name"] if uuid else query
        print(f"\nSearching transactions for: {target_name}")
        found = 0
        tree2 = ET.parse(xml_path)
        root2 = tree2.getroot()
        client2 = root2 if root2.tag == "client" else root2.find("client") or root2
        for portfolio in client2.findall(".//portfolios/portfolio"):
            pname = portfolio.findtext("name", "?")
            for tx in portfolio.findall(".//transactions/portfolio-transaction"):
                sec_el = tx.find("security")
                if sec_el is None:
                    continue
                # show any transaction whose security element has uuid matching
                # OR whose reference resolves to our target
                sec_uuid_inline = sec_el.findtext("uuid", "")
                ref_attr        = sec_el.get("reference", "")
                match = (uuid and sec_uuid_inline == uuid) or                         (uuid and pp._resolve_ref(client2, ref_attr, portfolio) == uuid)
                if match or (not uuid and query.lower() in ET2.tostring(sec_el, encoding="unicode").lower()):
                    print(f"\n  Portfolio: {pname}")
                    print(f"  TX raw XML snippet:")
                    raw = ET2.tostring(tx, encoding="unicode")[:600]
                    print("  " + raw[:600].replace("><", ">\n  <"))
                    found += 1
                    if found >= 5:
                        break
            if found >= 5:
                break
        if found == 0:
            print("  No matching transactions found.")
            print("  This confirms a reference resolution failure.")
            print("  Try running with a security you know HAS transactions to")
            print("  compare the XML structure.")
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
