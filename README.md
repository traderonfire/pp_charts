# pp_charts — Per-Asset Cash Flow Charts for Portfolio Performance

A Python script that reads a [Portfolio Performance](https://www.portfolio-performance.info) XML file and produces the same **Value / Invested Capital / Delta (P&L)** chart that PP shows for accounts — but scoped to any individual security or combination of securities you choose.

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
# List all securities in your file
python pp_charts.py myportfolio.xml --list

# Chart a single security (interactive window)
python pp_charts.py myportfolio.xml --assets "Vanguard Total Stock Market"

# Combine multiple securities into one chart
python pp_charts.py myportfolio.xml --assets "VTI" "VXUS" "BND"

# Restrict to a specific account
python pp_charts.py myportfolio.xml --assets "VTI" --account "Fidelity"

# Custom date range
python pp_charts.py myportfolio.xml --assets "VTI" --from 2020-01-01 --to 2024-12-31

# Save to PNG instead of opening an interactive window
python pp_charts.py myportfolio.xml --assets "VTI" "VXUS" --save chart.png

# Generate one PNG per security (saved to a pp_charts/ subfolder)
python pp_charts.py myportfolio.xml --all
```

Partial name matching works — `"Vanguard Total"` will match `"Vanguard Total Stock Market Index Fund – ETF Shares"`.

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

1. **Securities and historical prices** are read from `<securities>` — including all manually-entered valuations, which are used for assets like real estate or private equity where there is no market price feed.
2. **Portfolio transactions** (BUY, SELL, DELIVERY_INBOUND, DELIVERY_OUTBOUND, TRANSFER_IN/OUT) are replayed day by day to track share holdings.
3. **Cash account transactions** (FEES, DIVIDENDS, TAXES, FEES_REFUND, TAX_REFUND, INTEREST) are included in the invested capital calculation — dividends and fee refunds reduce net cost, fees and taxes increase it.
4. **Price factor auto-detection** — PP's XML stores historical prices as raw integers whose scale factor is not written in the file. The script detects the correct factor automatically by cross-referencing raw price values against transaction-implied prices.
5. **Price mode detection** — for assets where the stored price represents the *total holding value* rather than a *per-share price* (common for real estate and other manually-valued assets), the script detects and handles this correctly.

---

## Diagnostics

If a chart looks wrong, two diagnostic flags help inspect the raw data:

```bash
# Print all price entries and their interpreted values for a specific security
python pp_charts.py myportfolio.xml --debug-prices

# Dump raw XML price v-values, detected factor, and transaction quotes for one security
python pp_charts.py myportfolio.xml --dump-xml-prices "VTI"
```

The `--dump-xml-prices` output shows the raw integer stored in the XML alongside what the auto-detection converts it to, making it easy to spot if the price factor is wrong.

---

## Limitations

- Requires the **XML** export of your PP file (not the binary `.portfolio` format). The XML export takes about one second in PP via **File → Save As → XML** and does not affect your working file.
- Historical prices must be stored in PP. For assets with a price feed this happens automatically. For manually-valued assets (real estate, private equity) you need to have entered periodic valuations in PP's Historical Prices view.
- All securities in one PP file are assumed to use the same price factor (this is always true for files created by a single PP version).
- Multi-currency portfolios: values are shown in each security's native currency. When combining securities in different currencies the chart values are not FX-converted.

---

## Known PP version compatibility

Tested against PP XML files from versions 0.65–0.70. The XML schema is stable; the script should work with any version that uses the standard `<client>` root element.

---

## Related

- [Portfolio Performance](https://www.portfolio-performance.info) — the application this script reads
- [PP Forum](https://forum.portfolio-performance.info) — community discussion
- [PP GitHub](https://github.com/portfolio-performance/portfolio) — source code and issue tracker

---

## Contributing

Bug reports and PRs welcome. If you find a PP file where the price factor auto-detection or price mode detection gives wrong results, please open an issue and attach the output of `--dump-xml-prices` for the affected security (no need to share the full file).

---

## Licence

MIT
