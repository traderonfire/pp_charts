# Native PP Integration: Per-Security Cash Flow Chart — PR Implementation Guide

This document describes how to add the Value / Invested Capital / Delta chart natively
to Portfolio Performance as an additional tab in the security information pane (the panel
at the bottom of the All Securities / Securities list views).

---

## Overview of the change

The existing information pane for a selected security has tabs:
**Chart | Historical Quotes | Transactions | Trades | Events | Data quality**

The proposal adds one new tab: **Cash Flows** (or **P&L Chart**, or similar).

When one or more securities are selected in the list, the Cash Flows tab shows:
- **Value** over time (market value of the position)
- **Invested Capital** over time (cumulative net cash deployed)  
- **Delta / P&L** over time (value minus invested capital)

This is identical to the chart already available at the account level
(`SecurityAccountView` → Chart tab), but scoped to the selected securities.

---

## Codebase orientation

The relevant source lives under `name.abuchen.portfolio.ui/src/name/abuchen/portfolio/ui/views/`.

| File | Role |
|---|---|
| `SecurityListView.java` | The "All Securities" view — hosts the top table and the bottom information pane with its tab folder |
| `SecuritiesChart.java` | Renders the existing price-history chart in the Chart tab |
| `SecurityDetailsViewer.java` | The right-hand details panel next to the chart |
| `StatementOfAssetsHistoryBuilder.java` (in `..charts/`) | Builds the account-level value/invested/delta series — **this is the logic to reuse** |

The key data model classes are in `name.abuchen.portfolio/`:

| Class | Role |
|---|---|
| `Portfolio` | A securities account — has `getTransactions()` |
| `PortfolioTransaction` | A single BUY/SELL/DELIVERY_* transaction with `getSecurity()`, `getShares()`, `getMonetaryAmount()` |
| `Security` | A security — has `getPrices()` returning `List<SecurityPrice>` |
| `SecurityPrice` | A date + quote pair |
| `Client` | Root object — has `getPortfolios()` and `getAccounts()` |
| `ClientSnapshot` | A point-in-time snapshot — less useful here since we need a time series |

---

## Step-by-step implementation

### Step 1 — Create the data builder

Create a new class `SecurityCashFlowsBuilder` (or extend `StatementOfAssetsHistoryBuilder`)
in `name.abuchen.portfolio.ui/src/name/abuchen/portfolio/ui/views/charts/`.

```java
package name.abuchen.portfolio.ui.views.charts;

import java.time.LocalDate;
import java.util.*;
import name.abuchen.portfolio.model.*;
import name.abuchen.portfolio.money.CurrencyConverter;
import name.abuchen.portfolio.util.Interval;

/**
 * Builds daily time-series data for the per-security cash flow chart:
 * market value, invested capital (net deposits), and delta (P&L).
 *
 * Mirrors the logic in StatementOfAssetsHistoryBuilder but filtered
 * to a specific set of securities rather than an entire account.
 */
public class SecurityCashFlowsBuilder
{
    public static class DailyValues
    {
        public LocalDate date;
        public long value;           // market value in hecto units
        public long investedCapital; // net cash deployed in hecto units (can be negative)
        public long delta;           // value - investedCapital
    }

    private final Client client;
    private final CurrencyConverter converter;
    private final List<Security> securities;

    public SecurityCashFlowsBuilder(Client client, CurrencyConverter converter,
                                    List<Security> securities)
    {
        this.client     = client;
        this.converter  = converter;
        this.securities = securities;
    }

    public List<DailyValues> calculate(Interval interval)
    {
        Set<Security> securitySet = new HashSet<>(securities);

        // --- collect all relevant portfolio transactions ---
        // Group by date for efficient replay
        Map<LocalDate, List<PortfolioTransaction>> txByDate = new TreeMap<>();
        for (Portfolio portfolio : client.getPortfolios())
        {
            for (PortfolioTransaction tx : portfolio.getTransactions())
            {
                if (!securitySet.contains(tx.getSecurity()))
                    continue;
                txByDate.computeIfAbsent(tx.getDate(), k -> new ArrayList<>()).add(tx);
            }
        }

        // --- collect cash account transactions (fees, dividends, etc.) ---
        Map<LocalDate, List<AccountTransaction>> acctTxByDate = new TreeMap<>();
        for (Account account : client.getAccounts())
        {
            for (AccountTransaction tx : account.getTransactions())
            {
                if (tx.getSecurity() == null || !securitySet.contains(tx.getSecurity()))
                    continue;
                // Only types that affect invested capital
                switch (tx.getType())
                {
                    case FEES:
                    case FEES_REFUND:
                    case TAXES:
                    case TAX_REFUND:
                    case DIVIDENDS:
                    case INTEREST:
                    case INTEREST_CHARGE:
                        acctTxByDate.computeIfAbsent(tx.getDate(), k -> new ArrayList<>()).add(tx);
                        break;
                    default:
                        break;
                }
            }
        }

        // --- replay day by day over the interval ---
        Map<Security, Long> holdings       = new HashMap<>(); // shares in nano units
        Map<Security, Long> netInvested    = new HashMap<>(); // hecto units (can be negative)

        List<DailyValues> result = new ArrayList<>();
        LocalDate cursor = interval.getStart();

        while (!cursor.isAfter(interval.getEnd()))
        {
            // Process portfolio transactions on this day
            for (PortfolioTransaction tx : txByDate.getOrDefault(cursor, Collections.emptyList()))
            {
                long shares = tx.getShares();   // nano units
                long amount = tx.getAmount();   // hecto units (gross)
                Security sec = tx.getSecurity();

                switch (tx.getType())
                {
                    case BUY:
                    case DELIVERY_INBOUND:
                    case TRANSFER_IN:
                        holdings.merge(sec, shares, Long::sum);
                        netInvested.merge(sec, amount, Long::sum);  // cash out → invested up
                        break;

                    case SELL:
                    case DELIVERY_OUTBOUND:
                    case TRANSFER_OUT:
                        holdings.merge(sec, -shares, Long::sum);
                        netInvested.merge(sec, -amount, Long::sum); // cash in → invested down
                        break;

                    default:
                        break;
                }
            }

            // Process cash account transactions on this day
            for (AccountTransaction tx : acctTxByDate.getOrDefault(cursor, Collections.emptyList()))
            {
                long amount = tx.getAmount(); // hecto units
                Security sec = tx.getSecurity();

                switch (tx.getType())
                {
                    case FEES:
                    case TAXES:
                    case INTEREST_CHARGE:
                        netInvested.merge(sec, amount, Long::sum);   // cost → invested up
                        break;
                    case FEES_REFUND:
                    case TAX_REFUND:
                    case DIVIDENDS:
                    case INTEREST:
                        netInvested.merge(sec, -amount, Long::sum);  // receipt → invested down
                        break;
                    default:
                        break;
                }
            }

            // Calculate market value: shares × latest price
            long mkt = 0L;
            for (Security sec : securities)
            {
                long sharesHeld = holdings.getOrDefault(sec, 0L);
                if (sharesHeld <= 0)
                    continue;
                SecurityPrice price = sec.getSecurityPrice(cursor);
                if (price != null)
                {
                    // shares (nano) × price (Quote.factor) / (NANO/HECTO) → hecto
                    // Quote.factor = 100_000_000; NANO = 1_000_000_000; HECTO = 100
                    // simplify: shares * price / (NANO * Quote.factor / HECTO)
                    mkt += Math.round(sharesHeld * price.getValue()
                                      / (double) Values.Share.factor()
                                      / (double) Values.Quote.factor()
                                      * Values.Amount.factor());
                }
            }

            long inv = 0L;
            for (Security sec : securities)
                inv += netInvested.getOrDefault(sec, 0L);

            if (cursor.isAfter(interval.getStart()) || !result.isEmpty()
                            || mkt != 0 || inv != 0)
            {
                DailyValues dv = new DailyValues();
                dv.date           = cursor;
                dv.value          = mkt;
                dv.investedCapital = inv;
                dv.delta          = mkt - inv;
                result.add(dv);
            }

            cursor = cursor.plusDays(1);
        }

        return result;
    }
}
```

> **Note on currency conversion:** The above assumes all securities share the same base currency. For a multi-currency portfolio, wrap `tx.getMonetaryAmount()` through `converter.convert(cursor, amount)` before accumulating, using the same pattern as `StatementOfAssetsHistoryBuilder`.

---

### Step 2 — Create the chart widget

Create `SecurityCashFlowsChart.java` in the same views package, extending the existing `TimelineChart` (already used by `SecuritiesChart`).

```java
package name.abuchen.portfolio.ui.views;

import java.util.List;
import org.eclipse.swt.widgets.Composite;
import name.abuchen.portfolio.ui.util.chart.TimelineChart;
import name.abuchen.portfolio.ui.views.charts.SecurityCashFlowsBuilder;
import name.abuchen.portfolio.ui.views.charts.SecurityCashFlowsBuilder.DailyValues;

public class SecurityCashFlowsChart extends TimelineChart
{
    public SecurityCashFlowsChart(Composite parent)
    {
        super(parent);
        // TimelineChart sets up axes, zoom, date formatting, context menus
    }

    public void update(List<DailyValues> data, String currency)
    {
        clearAll(); // clear existing series

        double[] dates      = new double[data.size()];
        double[] values     = new double[data.size()];
        double[] invested   = new double[data.size()];
        double[] delta      = new double[data.size()];

        for (int i = 0; i < data.size(); i++)
        {
            DailyValues dv = data.get(i);
            dates[i]    = TimelineChart.toDouble(dv.date);
            // Convert from hecto to display units
            values[i]   = dv.value    / 100.0;
            invested[i] = dv.investedCapital / 100.0;
            delta[i]    = dv.delta    / 100.0;
        }

        // Add area fill for delta (green above zero, red below)
        addBaseLine(dates, new double[data.size()]); // zero baseline

        ILineSeries valueSeries = addDateSeries(
            dates, values, Messages.LabelValue, // add to Messages.properties
            ColorConstants.darkGreen); // purple in original; use theme colour
        valueSeries.setLineWidth(2);

        ILineSeries investedSeries = addDateSeries(
            dates, invested, Messages.LabelInvestedCapital,
            ColorConstants.orange);
        investedSeries.setLineWidth(2);

        ILineSeries deltaSeries = addDateSeries(
            dates, delta, Messages.LabelDelta,
            ColorConstants.blue);
        deltaSeries.setLineWidth(2);

        // Fill delta area (positive = green, negative = red)
        // PP's existing charts use RangeMarker or area fill — adapt as per
        // the pattern in StatementOfAssetsHistoryBuilder.buildChart()

        adjustRange();
    }
}
```

The exact SWT/SWTCHART API calls should mirror those in `SecuritiesChart.java` — look at how it adds line series and fills — to stay consistent with PP's existing chart style.

---

### Step 3 — Add the tab to SecurityListView

In `SecurityListView.java`, find the method that creates the tab folder for the information pane (look for `CTabFolder` or `createBottomTable`). Add a new `CTabItem` alongside the existing Chart tab:

```java
// Inside the method that builds the information pane tab folder:

CTabItem cashFlowsTab = new CTabItem(tabFolder, SWT.NONE);
cashFlowsTab.setText(Messages.LabelCashFlowsChart); // add to Messages.properties
cashFlowsTab.setImage(/* reuse an existing chart icon */);

Composite cashFlowsComposite = new Composite(tabFolder, SWT.NONE);
cashFlowsComposite.setLayout(new FillLayout());
cashFlowsTab.setControl(cashFlowsComposite);

SecurityCashFlowsChart cashFlowsChart = new SecurityCashFlowsChart(cashFlowsComposite);
```

Then in the selection listener that fires when a security is selected or the tab is switched:

```java
private void updateCashFlowsChart(List<Security> selected)
{
    if (selected == null || selected.isEmpty())
    {
        cashFlowsChart.clearAll();
        return;
    }

    // Determine interval: from first transaction to today
    Interval interval = computeIntervalForSecurities(selected); // helper method

    SecurityCashFlowsBuilder builder = new SecurityCashFlowsBuilder(
        getClient(),
        getClient().getBaseCurrency(), // or converter
        selected);

    List<DailyValues> data = builder.calculate(interval);
    cashFlowsChart.update(data, getClient().getBaseCurrency());
}
```

Hook `updateCashFlowsChart` into:
1. The existing `selectionChanged` listener (fires when the user selects different securities)
2. The tab folder's `selectionListener` (fires when the user switches to the Cash Flows tab — to avoid computing it until needed)

---

### Step 4 — Add message strings

In `name.abuchen.portfolio.ui/src/name/abuchen/portfolio/ui/Messages.properties` (and all language variants), add:

```
LabelCashFlowsChart = Cash Flows
LabelValue = Value
LabelInvestedCapital = Invested Capital
LabelDelta = Delta (P\&L)
```

And in `Messages.java` (the generated constants class):

```java
public static String LabelCashFlowsChart;
public static String LabelValue;
public static String LabelInvestedCapital;
public static String LabelDelta;
```

---

### Step 5 — Extend to security accounts view

The same tab should appear in `SecurityAccountView.java` (the per-account information pane), scoped to securities within that account. The builder already accepts a `List<Security>` so this is straightforward — filter `client.getPortfolios()` to the selected account when collecting transactions.

---

## Key design decisions to discuss with @buchen

**1. Multi-security selection.** The existing Chart tab shows each selected security as a separate price line. The Cash Flows tab should combine them into a single set of three lines (total value, total invested, total delta). Make this explicit in the PR description. There is an open PR (#multiple_securities_chart by @OnkelDok) adding per-security lines to the existing chart — coordinate to avoid conflicts.

**2. Reporting period integration.** PP has a global reporting period selector. The Cash Flows chart is most useful when it always starts from the first transaction regardless of the reporting period (since it shows the full investment journey). Discuss whether to respect the global period or always show the full history. The simplest PR respects the global period; a follow-up can add a "Show full history" toggle.

**3. Currency handling.** For multi-currency portfolios, decide whether to show values in:
   - The security's native currency (simplest; works well for single-security view)
   - The portfolio base currency (requires FX conversion; better for multi-security groups)
   Use `CurrencyConverter` for the latter — the same approach as `StatementOfAssetsHistoryBuilder`.

**4. Performance.** Computing daily values for all 365 × N days can be slow for large portfolios. The builder should only recalculate when the tab is visible and the selection changes. Use lazy evaluation — do not compute in the selection listener, compute in the tab's `visibleChanged` event.

**5. Price mode (total vs per-share).** For manually-valued assets (real estate etc.), PP may store total holding value as the price rather than a per-share price. The `Security.getSecurityPrice()` API returns the raw price — whether to multiply by shares or use directly depends on how the user entered data. Discuss with @buchen whether PP has an internal flag for this or whether it should be detected heuristically (as the Python script does).

---

## Files to create / modify

| Action | File |
|---|---|
| **Create** | `name.abuchen.portfolio.ui/.../views/charts/SecurityCashFlowsBuilder.java` |
| **Create** | `name.abuchen.portfolio.ui/.../views/SecurityCashFlowsChart.java` |
| **Modify** | `name.abuchen.portfolio.ui/.../views/SecurityListView.java` |
| **Modify** | `name.abuchen.portfolio.ui/.../views/SecurityAccountView.java` (optional, same pattern) |
| **Modify** | `name.abuchen.portfolio.ui/.../Messages.properties` (and language variants) |
| **Modify** | `name.abuchen.portfolio.ui/.../Messages.java` |
| **Create** | `name.abuchen.portfolio.ui/.../views/charts/SecurityCashFlowsBuilderTest.java` (unit tests) |

---

## How to set up the development environment

1. Clone the repo: `git clone https://github.com/portfolio-performance/portfolio`
2. Import into Eclipse as an existing Maven project (the codebase is Eclipse RCP / OSGi)
3. Install the Eclipse PDE (Plugin Development Environment) if not already installed
4. Run target: **portfolio-target-definition** → Set as Active Target Platform
5. Launch configuration: **name.abuchen.portfolio.ui** → Run As → Eclipse Application

Full dev setup instructions: https://github.com/portfolio-performance/portfolio/blob/master/CONTRIBUTING.md

---

## Suggested PR title and description template

**Title:** `feat: add Cash Flows tab to security information pane (value/invested capital/delta)`

**Description:**
```
### Motivation

The account-level chart (Statement of Assets → Chart) shows Value, Invested Capital
and Delta over time — a powerful way to see how an investment is actually performing
including all cash flows. This view is not available at the security level.

This PR adds a "Cash Flows" tab to the security information pane in the All Securities
view, showing the same three lines scoped to the selected security or securities.

Closes #XXXX  ← link to feature request issue

### What changes

- New `SecurityCashFlowsBuilder` computes daily value/invested capital/delta series
  for an arbitrary list of securities, replaying portfolio and cash account transactions.
- New `SecurityCashFlowsChart` renders the result using the existing TimelineChart.
- `SecurityListView` gets a new "Cash Flows" tab alongside the existing "Chart" tab.

### Screenshots

[attach before/after screenshots]

### Notes for review

- Currency conversion: currently uses native currency per security; FX conversion
  can be added in a follow-up.
- Reporting period: respects the global period selector.
- Performance: chart is computed lazily on tab selection, not on every security click.
```
