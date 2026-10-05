# Data and display contract

Reviewed on 2026-10-05 against the official
[Twelve Data API documentation](https://twelvedata.com/docs),
[request guide](https://support.twelvedata.com/en/articles/5620512-how-to-create-a-request),
and [RunCat Neo Custom Metrics schema](https://github.com/runcat-dev/RunCatNeo/blob/main/docs/CustomMetricsSchema.md).

## One request, one daily baseline

Request: `GET https://api.twelvedata.com/quote?symbol=USD%2FKRW&interval=1day&format=JSON`
with the key in the documented `Authorization: apikey …` header. `1day` is explicit,
not merely the current API default. `/quote` is documented as one API credit per
symbol. Account entitlement is checked on the user's machine; it was not tested
with the user's key during development. No key was received.

Use `close` for KRW per USD and **the same response's `percent_change`** for
comparison with `previous_close`. The documented identity is
`(close - previous_close) / previous_close * 100`. We do not round the price first,
reconstruct yesterday's price from local runs, query a second daily series,
substitute another data vendor, or divide the percentage by 100 a second time.

The reference is Twelve Data's preceding daily bar, not a fixed Korean clock
instant or elapsed 24-hour baseline. No date for that preceding bar is fabricated
when the quote response does not supply it. Calendar boundaries and market
holidays remain provider-defined. `timestamp` is the
opening timestamp of the selected daily bar; **`last_quote_at`** is the last
minute candle's Unix timestamp. Only `last_quote_at` supplies `Quote Time` and
quote age, not necessarily the time of an individual tick trade. If absent/null,
show `Quote Time: —` and separately label `timestamp` as `Bar Start`; never
pretend the start of the day is the latest quote time. `close` is the
provider's latest close field, not a bank buy/sell quotation. We do not claim
byte-for-byte equivalence with the former `/exchange_rate` feed.

## Validation and partial results

Core requirements: exact `symbol=USD/KRW`, a finite positive `close`, an integer
bar-opening `timestamp` in the supported range (2000 onward; at most 300 seconds
ahead of the local clock), and `interval=1day` if the optional response field is present.
The fixed request is what selects the bar interval. If supplied and non-null,
`last_quote_at` must satisfy the same integer/range checks; an invalid value is
not silently discarded. Booleans are not measurements.
Duplicate JSON keys, NaN/Infinity, invalid encodings, malformed responses, and
oversized bodies fail without replacing the metric. Decimal limits constrain
formatting/computation, not permissible economic outcomes.

A finite positive `previous_close` and finite `percent_change` are needed for a
comparison. Their documented formula is checked with intervals reflecting the
precision of the provider's fields. This tolerates honest decimal rounding
without equating substantially conflicting values. The provider's percentage
remains the displayed value; it is not silently replaced with our own calculation.
If a comparison field is absent/invalid or inconsistent, show `—`, never 0%.
The normal sampler can still display the valid current price and a status row.
The installer's `--require-change` check instead rejects partial results.

Optional `change`, open/high/low, names, account identifiers, free-form error
messages and other backend fields do not determine the display and are not
copied into local diagnostics or snapshots. `is_market_open` is accepted only
when it is an actual boolean. It controls a descriptive status row, not a
locally invented trading calendar.

## Formatting

Menu: `₩1,344 ▲0.28%`; card rate: `₩1,343.87`. Rates and percentage are rounded
with decimal ROUND_HALF_UP. Two percentage decimals; no extra +/− after the
triangle. If the rounded change is zero, display `0.00%` with no triangle, even
for a tiny negative raw value. A missing comparison displays `₩1,344 —`.
No percentage progress bar or color/font-size claims are made.

Normal rows are `1 USD`, `Change`, `Quote Time`. The last-quote time is converted from epoch to
the Mac's local timezone and includes its zone name/offset (KST on the user's
current Mac setting). `lastUpdatedDate` is the file-write time in UTC and is not
the market quote time. Closed markets get `Quote Status: Market closed`. An open
or unknown-state quote older than 15 minutes gets `Older quote (...)`. This is a
display-age threshold, not evidence of market closure or an API outage. Unknown
quote time produces an explicit status, not a guessed age from the bar opening. If the
sampler stops altogether, RunCat's last-updated footer remains the age indicator;
we do not run an additional stale-status timer.

Weekends, sleep and restart do not change the reference-price rule. A repeated
valid old market quote can yield a newly received/written snapshot; its old quote
time and closure/age row remain visible. Failed requests preserve prior bytes and
`lastUpdatedDate` and never insert guessed fresh numerical values.

The distributed `tests/fixtures/quote_synthetic.json` has entirely synthetic
numbers and follows public field names. It is not a recorded account response.
