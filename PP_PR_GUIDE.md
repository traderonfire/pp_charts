# Native PP Integration: Per-Security Cash Flow Chart — PR Implementation Guide

This document describes how to add the Value / Invested Capital / Delta chart natively
to Portfolio Performance as an additional tab in the security information pane (the panel
at the bottom of the All Securities / Securities list views).

A working Python proof-of-concept is available at [pp_charts repo link], validated
against a real 139-security portfolio with multiple accounts and broker types.

---

## Overview of the change

The existing information pane for a selected security has tabs:
**Chart | Historical Quotes | Transactions | Trades | Events | Data quality**

The proposal adds one new tab: **Cash Flows** (or **P&L Chart**, or similar).

When one or more securities are selected in the list, the Cash Flows tab shows:
- **Value** over time (market value of the position)
- **Invested Capital** over time (cumulative net cash deployed — can go negative after a profitable exit)
- **Delta / P&L** over time (value minus invested capital)

This is identical to the chart already available at the account level
(`SecurityAccountView` -> Chart tab), but scoped to the selected securities rather than
a whole account. When multiple securities are selected they are combined into a single
set of three lines — not one set per security.

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
| `AccountTransaction` | A cash account transaction (dividends, fees, taxes, etc.) with `getSecurity()` |
| `Security` | A security — has `getPrices()` returning `List<SecurityPrice>` |
| `SecurityPrice` | A date + quote pair |
| `Client` | Root object — has `getPortfolios()` and `getAccounts()` |

---

## Step-by-step implementation

### Step 1 — Create the data builder

Create a new class `SecurityCashFlowsBuilder` in
`name.abuchen.portfolio.ui/src/name/abuchen/portfolio/ui/views/charts/`.

```java
package name.abuchen.portfolio.ui.views.charts;

import java.time.LocalDate;
import java.util.*;
import name.abuchen.portfolio.model.*;
import name.abuchen.portfolio.money.CurrencyConverter;
import name.abuchen.portfolio.util.Interval;

public class SecurityCashFlowsBuilder
{
    public static class DailyValues
    {
        public LocalDate date;
        public long value;            // market value in hecto units
        public long investedCapital;  // net cash deployed in hecto units (can be negative)
        public long delta;            // value - investedCapital
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

        // client.getPortfolios() returns all Portfolio objects and their transactions
        // regardless of XStream serialization order. The Java object model resolves
        // all crossEntry references transparently — no need to handle the two
        // serialization variants (portfolio-primary vs account-primary) explicitly.
        Map<LocalDate, List<PortfolioTransaction>> txByDate = new TreeMap<>();
        for (Portfolio portfolio : client.getPortfolios())
            for (PortfolioTransaction tx : portfolio.getTransactions())
            {
                if (!securitySet.contains(tx.getSecurity())) continue;
                txByDate.computeIfAbsent(tx.getDate(), k -> new ArrayList<>()).add(tx);
            }

        Map<LocalDate, List<AccountTransaction>> acctTxByDate = new TreeMap<>();
        for (Account account : client.getAccounts())
            for (AccountTransaction tx : account.getTransactions())
            {
                if (tx.getSecurity() == null || !securitySet.contains(tx.getSecurity())) continue;
                switch (tx.getType()) {
                    case FEES: case FEES_REFUND: case TAXES: case TAX_REFUND:
                    case DIVIDENDS: case INTEREST: case INTEREST_CHARGE:
                        acctTxByDate.computeIfAbsent(tx.getDate(), k -> new ArrayList<>()).add(tx);
                        break;
                    default: break;
                }
            }

        Map<Security, Long> holdings    = new HashMap<>();
        Map<Security, Long> netInvested = new HashMap<>();
        List<DailyValues>   result      = new ArrayList<>();
        LocalDate cursor = interval.getStart();

        while (!cursor.isAfter(interval.getEnd()))
        {
            // Sort same-day transactions: OUTBOUNDs before INBOUNDs.
            // This prevents momentary zero-holdings during same-day broker transfers,
            // which would cause a spurious value=0 in the chart for that day.
            List<PortfolioTransaction> dayTxs = new ArrayList<>(
                txByDate.getOrDefault(cursor, Collections.emptyList()));
            dayTxs.sort(Comparator.comparingInt(tx -> {
                switch (tx.getType()) {
                    case SELL: case DELIVERY_OUTBOUND: case TRANSFER_OUT: return 0;
                    default: return 1;
                }
            }));

            for (PortfolioTransaction tx : dayTxs)
            {
                long shares = tx.getShares();
                long amount = tx.getAmount();
                Security sec = tx.getSecurity();

                switch (tx.getType())
                {
                    case BUY:
                    case DELIVERY_INBOUND:
                    case TRANSFER_IN:
                        // DELIVERY_INBOUND is treated identically to BUY.
                        // Users commonly record purchases as deliveries when not
                        // maintaining a paired cash account. Broker-to-broker transfer
                        // pairs (DELIVERY_OUTBOUND + matching DELIVERY_INBOUND on the
                        // same day for the same amount) net to zero automatically.
                        holdings.merge(sec, shares, Long::sum);
                        netInvested.merge(sec, amount, Long::sum);
                        break;

                    case SELL:
                    case DELIVERY_OUTBOUND:
                    case TRANSFER_OUT:
                        holdings.merge(sec, -shares, Long::sum);
                        netInvested.merge(sec, -amount, Long::sum);
                        break;

                    default: break;
                }
            }

            for (AccountTransaction tx : acctTxByDate.getOrDefault(cursor, Collections.emptyList()))
            {
                // TODO: apply converter.convert(cursor, tx.getMonetaryAmount()) for
                // multi-currency portfolios (same pattern as StatementOfAssetsHistoryBuilder)
                long amount = tx.getAmount();
                Security sec = tx.getSecurity();

                switch (tx.getType()) {
                    case FEES: case TAXES: case INTEREST_CHARGE:
                        netInvested.merge(sec, amount, Long::sum);
                        break;
                    case FEES_REFUND: case TAX_REFUND: case DIVIDENDS: case INTEREST:
                        netInvested.merge(sec, -amount, Long::sum);
                        break;
                    default: break;
                }
            }

            long mkt = 0L;
            for (Security sec : securities)
            {
                long sharesHeld = holdings.getOrDefault(sec, 0L);
                if (sharesHeld <= 0) continue;
                SecurityPrice price = sec.getSecurityPrice(cursor);
                if (price != null)
                    mkt += Math.round(sharesHeld * price.getValue()
                                      / (double) Values.Share.factor()
                                      / (double) Values.Quote.factor()
                                      * Values.Amount.factor());
            }

            long inv = 0L;
            for (Security sec : securities)
                inv += netInvested.getOrDefault(sec, 0L);

            DailyValues dv  = new DailyValues();
            dv.date          = cursor;
            dv.value         = mkt;
            dv.investedCapital = inv;
            dv.delta         = mkt - inv;
            result.add(dv);
            cursor = cursor.plusDays(1);
        }

        return result;
    }
}
```

---

### Step 2 — Create the chart widget

Create `SecurityCashFlowsChart.java` extending the existing `TimelineChart`:

```java
public class SecurityCashFlowsChart extends TimelineChart
{
    public SecurityCashFlowsChart(Composite parent) { super(parent); }

    public void update(List<DailyValues> data, String currency)
    {
        clearAll();
        double[] dates = new double[data.size()];
        double[] values = new double[data.size()];
        double[] invested = new double[data.size()];
        double[] delta = new double[data.size()];

        for (int i = 0; i < data.size(); i++) {
            DailyValues dv = data.get(i);
            dates[i]    = TimelineChart.toDouble(dv.date);
            values[i]   = dv.value           / 100.0;
            invested[i] = dv.investedCapital / 100.0;
            delta[i]    = dv.delta           / 100.0;
        }

        addBaseLine(dates, new double[data.size()]);
        addDateSeries(dates, values,   Messages.LabelValue,          ColorConstants.darkGreen).setLineWidth(2);
        addDateSeries(dates, invested, Messages.LabelInvestedCapital, ColorConstants.orange).setLineWidth(2);
        addDateSeries(dates, delta,    Messages.LabelDelta,           ColorConstants.blue).setLineWidth(2);
        // Area fill behind delta — follow pattern in StatementOfAssetsHistoryBuilder.buildChart()
        adjustRange();
    }
}
```

Mirror the exact SWT/SWTCHART API calls from `SecuritiesChart.java` for consistency.

---

### Step 3 — Add the tab to SecurityListView

```java
CTabItem cashFlowsTab = new CTabItem(tabFolder, SWT.NONE);
cashFlowsTab.setText(Messages.LabelCashFlowsChart);

Composite cashFlowsComposite = new Composite(tabFolder, SWT.NONE);
cashFlowsComposite.setLayout(new FillLayout());
cashFlowsTab.setControl(cashFlowsComposite);

SecurityCashFlowsChart cashFlowsChart = new SecurityCashFlowsChart(cashFlowsComposite);
```

Update lazily in the tab selection listener:

```java
private void updateCashFlowsChart(List<Security> selected)
{
    if (selected == null || selected.isEmpty()) { cashFlowsChart.clearAll(); return; }
    Interval interval = computeIntervalForSecurities(selected);
    SecurityCashFlowsBuilder builder = new SecurityCashFlowsBuilder(getClient(), converter, selected);
    cashFlowsChart.update(builder.calculate(interval), getClient().getBaseCurrency());
}
```

Hook into the `selectionChanged` listener AND the tab folder's `selectionListener`
(so the chart is only computed when the tab is actually visible).

---

### Step 4 — Add message strings

In `Messages.properties` (and all language variants):

```
LabelCashFlowsChart = Cash Flows
LabelValue = Value
LabelInvestedCapital = Invested Capital
LabelDelta = Delta (P\&L)
```

---

### Step 5 — Extend to security accounts view

Same pattern in `SecurityAccountView.java`, filtering transactions to the selected
account. The builder's `List<Security>` parameter already supports this.

---

## Key design decisions to discuss with @buchen

**1. Multi-security selection.** Combine selected securities into a single set of three
lines (total value, total invested, total delta). Coordinate with the open PR by
@OnkelDok to avoid conflicts in `SecurityListView.java`.

**2. Reporting period.** The Cash Flows chart is most useful starting from the first
transaction (showing the full investment journey). Suggest always showing full history,
with a follow-up option to respect the global period selector.

**3. Currency handling.** Apply `CurrencyConverter` to account transaction amounts for
multi-currency portfolios — same pattern as `StatementOfAssetsHistoryBuilder`. Without
this, dividends or fees paid in a different currency are included at face value.

**4. Performance.** Compute lazily on tab selection. For portfolios with long daily
price histories the day-by-day replay can take noticeable time — cache the result and
invalidate only when the selection or underlying data changes.

---

## Files to create / modify

| Action | File |
|---|---|
| **Create** | `name.abuchen.portfolio.ui/.../views/charts/SecurityCashFlowsBuilder.java` |
| **Create** | `name.abuchen.portfolio.ui/.../views/SecurityCashFlowsChart.java` |
| **Modify** | `name.abuchen.portfolio.ui/.../views/SecurityListView.java` |
| **Modify** | `name.abuchen.portfolio.ui/.../views/SecurityAccountView.java` (optional) |
| **Modify** | `name.abuchen.portfolio.ui/.../Messages.properties` (and language variants) |
| **Modify** | `name.abuchen.portfolio.ui/.../Messages.java` |
| **Create** | `name.abuchen.portfolio.ui/.../views/charts/SecurityCashFlowsBuilderTest.java` |

---

## Development environment setup

1. `git clone https://github.com/portfolio-performance/portfolio`
2. Import into Eclipse as an existing Maven project (Eclipse RCP / OSGi)
3. Install Eclipse PDE
4. Set **portfolio-target-definition** as the Active Target Platform
5. Run As → Eclipse Application from `name.abuchen.portfolio.ui`

Full instructions: https://github.com/portfolio-performance/portfolio/blob/master/CONTRIBUTING.md

---

## Suggested PR title and description

**Title:** `feat: add Cash Flows tab to security information pane (value/invested capital/delta)`

**Body:**
```
### Motivation

The account-level chart (Statement of Assets -> Chart) shows Value, Invested Capital
and Delta over time. This view is not available at the security level.

This PR adds a "Cash Flows" tab to the security information pane, showing the same
three lines scoped to the selected security or group of securities.

Closes #XXXX

### What changes

- New SecurityCashFlowsBuilder computes daily value/invested capital/delta series
  for an arbitrary list of securities.
- New SecurityCashFlowsChart renders the result using the existing TimelineChart.
- SecurityListView gets a new "Cash Flows" tab.

### Transaction handling

DELIVERY_INBOUND/OUTBOUND and TRANSFER_IN/OUT are treated the same as BUY/SELL
for invested capital. Users commonly record purchases as deliveries when not tracking
a paired cash account. Same-day broker transfer pairs net to zero automatically.

### Notes

- Currency: face-value inclusion; FX conversion via CurrencyConverter in follow-up.
- Reporting period: full history from first transaction.
- Performance: lazily computed on tab selection.
```
