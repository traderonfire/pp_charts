# pp_charts — Per-Asset Cash Flow Charts for Portfolio Performance

A Python script that reads a [Portfolio Performance](https://www.portfolio-performance.info) XML file and produces the same **Value / Invested Capital / Delta (P&L)** chart that PP shows for accounts — but scoped to any individual security or combination of securities you choose.

![Example chart showing Value, Invested Capital and Delta lines for a real estate asset over time](https://raw.githubusercontent.com/your-username/pp-charts/main/docs/example_chart.png)

---

## The problem this solves

Portfolio Performance shows an excellent cash-flow chart (value over time, net invested capital, and the P&L gap between them) at the **account** and **whole-portfolio** level. There is currently no way to get this same chart for:

- A **single security** (e.g. "how is my VTI position actually doing, including all cash flows?")
- A **custom group** of securities across one or more accounts (e.g. "show me my bond ETFs combined")
- With an **independent time range** per saved view

This script fills that gap by reading your PP data directly and producing publication-quality charts.

---

## Requirements

```bash
pip install pandas matplotlib lxml
```

Python 3.10 or later.

---

## Usage

First, export your portfolio from PP as XML: **File → Save As → XML** (your working `.portfolio` binary file is untouched).

```bash
# List all securities with their tickers and ISINs
python pp_charts.py myportfolio.xml --list

# Chart by ticker, ISIN, full name, or partial name — all work
python pp_charts.py myportfolio.xml --assets VTI
python pp_charts.py myportfolio.xml --assets US9229087690
python pp_charts.py myportfolio.xml --assets "Vanguard Total Stock Market"

# Combine multiple securities into one chart
python pp_charts.py myportfolio.xml --assets VTI VXUS BND

# Restrict to a specific account
python pp_charts.py myportfolio.xml --assets VTI --account "Fidelity"

# Custom date range
python pp_charts.py myportfolio.xml --assets VTI --from 2020-01-01 --to 2024-12-31

# Save to PNG instead of opening an interactive window
python pp_charts.py myportfolio.xml --assets VTI VXUS --save chart.png

# Generate one PNG per security (saved to a pp_charts/ subfolder)
python pp_charts.py myportfolio.xml --all
```

Assets can be identified by **ticker symbol**, **ISIN**, **full name**, or **partial name** — the script tries each in that order. `--list` shows all three identifiers in a table so you can see exactly what's available.

---

## What the chart shows

The chart reproduces PP's account-level cash flow view for your chosen security or group:

| Line | Colour | Meaning |
|---|---|---|
| **Value** | Purple | Market value of the position at each date (shares × price, or total valuation for manually-priced assets such as real estate) |
| **Invested Capital** | Gold | Cumulative net cash deployed: buys and fees paid in, minus sale proceeds, dividends received, and fee refunds. Can go negative after a profitable exit. |
| **Delta (P&L)** | Blue | Value minus Invested Capital — the unrealised (or total, post-sale) profit or loss |

The green/red fill behind the Delta line makes positive and negative P&L immediately visible.

---

## How it works

The script parses the PP XML file directly, without launching PP:

1. **Securities and historical prices** are read from `<securities>` — including all manually-entered valuations for assets like real estate or private equity where there is no market price feed.
2. **Portfolio transactions** (BUY, SELL, DELIVERY_INBOUND, DELIVERY_OUTBOUND, TRANSFER_IN/OUT) are replayed day by day to track share holdings and invested capital. `DELIVERY_INBOUND` and `DELIVERY_OUTBOUND` are treated the same as `BUY` and `SELL` — users commonly record purchases and sales as deliveries when not maintaining a paired cash account. Same-day broker transfer pairs net to zero automatically.
3. **XStream serialization variants** — PP's XML uses [XStream](https://x-stream.github.io/) and serializes `BuySellEntry` objects with either the portfolio or the account as the primary element, cross-referencing the other via XPath `reference=` attributes. The script handles both variants and builds a full parent map to resolve ancestor chains correctly.
4. **Cash account transactions** (FEES, DIVIDENDS, TAXES, FEES_REFUND, TAX_REFUND, INTEREST) are included in the invested capital calculation — dividends and fee refunds reduce net cost, fees and taxes increase it.
5. **Price factor auto-detection** — PP's XML stores historical prices and share counts as raw integers whose scale factors are not written in the file. The script auto-detects the correct factors by cross-referencing raw values against transaction-implied prices and quantities.
6. **Price mode detection** — for assets where the stored price represents the *total holding value* rather than a *per-share price* (common for real estate and other manually-valued assets), the script detects and handles this correctly.

---

## Diagnostics

Several flags help inspect the raw data when a chart looks wrong or a security shows no transactions:

```bash
# List all accounts and portfolios found, with transaction counts
python pp_charts.py myportfolio.xml --list-accounts

# List all parsed transactions for a security in date order
python pp_charts.py myportfolio.xml --list-tx VTI

# Show all securities with detected price/share factors and interpreted values
python pp_charts.py myportfolio.xml --debug-prices

# Dump raw XML price v-values and detected factors for one security
python pp_charts.py myportfolio.xml --dump-xml-prices VTI

# Search the raw XML for all transactions referencing a security
# (use when a security appears in --list but shows no chart data)
python pp_charts.py myportfolio.xml --dump-raw-tx VTI

# Show the full XML ancestor chain for a specific transaction UUID
# (use when transactions appear under the wrong account name)
python pp_charts.py myportfolio.xml --dump-ancestors TRANSACTION-UUID
```

Start with `--list-tx` — it shows every transaction the script found, with date, type, shares, gross amount, and account name. Compare against PP's own transaction list to spot what's missing or wrong. Use `--dump-raw-tx` if transactions are missing entirely, and `--dump-ancestors` if account names look wrong.

---

## Limitations

- Requires the **XML** export of your PP file (not the binary `.portfolio` format). The XML export takes about one second in PP via **File → Save As → XML** and does not affect your working file.
- Historical prices must be stored in PP. For assets with a price feed this happens automatically. For manually-valued assets (real estate, private equity) you need to have entered periodic valuations in PP's Historical Prices view.
- Multi-currency portfolios: cash account transactions (dividends, fees) are included at face value without FX conversion. For portfolios where these amounts are in a different currency from the security, the invested capital figure will be approximate.
- The share and price scale factors are auto-detected from the data, which requires at least one transaction with a known gross amount for each security. Securities with no transactions (watchlist-only) have their factors estimated from the global portfolio evidence.

---

## Known PP version compatibility

Tested against PP XML files from versions 0.65–0.70. The XML schema is stable; the script should work with any version that uses the standard `<client>` root element.

---

## Related

- [Portfolio Performance](https://www.portfolio-performance.info) — the application this script reads
- [PP Forum](https://forum.portfolio-performance.info) — community discussion
- [PP GitHub](https://github.com/portfolio-performance/portfolio) — source code and issue tracker
- [Feature request: per-security cash flow chart](https://github.com/portfolio-performance/portfolio/issues/) — the upstream issue this script works around

---

## Contributing

Bug reports and PRs welcome. When reporting an issue, please include:

- For **wrong values**: the output of `--list-tx TICKER` and `--dump-xml-prices TICKER`
- For **missing transactions**: the output of `--dump-raw-tx TICKER`
- For **wrong account names**: the output of `--dump-ancestors TRANSACTION-UUID` for one of the affected transactions

No need to share your full portfolio file — the diagnostic output contains enough information to reproduce the problem.

---

## Licence

MIT
