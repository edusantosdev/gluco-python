# Time in range (TIR) vs last week — design notes

Background for a possible future feature: show **time in range %** and a **comparison to the prior week** (e.g. delta in percentage points).

## Current limitations in this repo

- The daemon keeps only **~12 hours** of glucose samples in memory (`graph_data` from LibreView plus the current reading), clamped by `_HISTORY_RETAIN_HOURS` in `daemon/service.py`.
- There is **no** TIR calculation or weekly aggregation today.
- **You cannot** compute “this week vs last week” from that in-memory history alone—you need **at least two week-scale windows** of data, ideally **14+ days** for a full rolling comparison.

## Defining TIR and “in range”

- Standard CGM **TIR** is often **70–180 mg/dL**, which matches this project’s **green** band (see `README.md` / `ENGINEERING.md`).
- A reasonable rule: count a reading as in range when `low <= value <= high`, aligned with **`ColorThresholds`** / menubar coloring so UI and stats agree.

### What “compared to last week” means

Pick one definition and stick to it:

1. **Rolling windows (recommended for implementation simplicity):** e.g. last **7×24 hours** vs the **previous** 7×24 hours. Same length, easy to explain.
2. **Calendar weeks (ISO, Mon–Sun):** “this week so far” vs “last full calendar week.” More complex at boundaries.

**Comparison metric:** usually **difference in percentage points** (pp), e.g. this window 72%, prior window 65% → **+7 pp**. Avoid wording like “+7%” meaning relative change unless you explicitly want `((72/65) - 1) * 100`.

## Recommended implementation path

1. **Persist samples locally** under the daemon state dir (e.g. `~/.glucobar/`), alongside `session.json`. On each successful poll, **upsert** `(utc_timestamp, value_mg_dl)` from normalized history and the current point; **dedupe** on timestamp.
   - SQLite is a good fit for querying arbitrary time ranges.
2. **Compute TIR on read (or on a slow timer):** for each window,  
   `TIR = (count of in-range readings) / (total readings with known value)`.  
   Optional later: **time-weighted** TIR using intervals between readings.
3. **Daemon HTTP API:** e.g. `GET /metrics/tir` returning fields such as  
   `this_period_pct`, `prior_period_pct`, `delta_pp`, plus maybe `sample_count` per window so the UI can show low confidence when data is sparse.
4. **Menubar app:** extend `macos_frontend/api_client.py` (`DaemonClient`) and show a line in the dropdown menu, same pattern as `/glucose/current` and `/glucose/graph`.

### Caveats

- Stats reflect **only intervals when GlucoBar was running** and polling succeeded; laptop sleep or daemon stopped **creates gaps**. Many apps report “% of captured readings” rather than true clock-time coverage unless gaps are modeled explicitly.

## Alternative: LibreView-reported stats

If **`pylibrelinkup`** (or another Abbott/LibreView API surface) exposes **pre-built reports** with official TIR over longer ranges, you could surface that instead of locally aggregated data. The current code path uses **`read(patient)`** and **`graph_data`**, which is **short-horizon**; any such feature would need explicit verification against the library and API.

## Related files

- `daemon/service.py` — polling, history normalization, retention window
- `daemon/state.py` — in-memory `history`
- `daemon/main.py` — HTTP routes
- `macos_frontend/api_client.py` — frontend → daemon HTTP client
- `macos_frontend/app.py` — `ColorThresholds`, menu construction
