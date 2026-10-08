# GPT 5.6 Terra prompt for Kibana 2 multi date search

Use the following prompt in a task with access to the actual application repository.

---

Inspect and, where the repository permits, implement a minimal change to our custom Kibana 2 fork so its search and count histogram can use an indexed array of dates in Elasticsearch 8. Start from the existing query and plotting paths. Keep the current architecture, plotting library and dependencies; avoid unrelated refactoring. Produce a focused patch and meaningful verification, not only a plan.

## Application context

- Velociraptor offline collector evidence is parsed from JSONL into Elasticsearch 8; inspect configuration to establish the installed minor version and client compatibility.
- There is one index per case and artifact source. An existing alias includes all source indices for a case. Reuse this alias for chart and table requests.
- A case may contain roughly 1,000 hosts with gigabytes of evidence per host. Do not download all matching documents to compute the histogram or introduce a runtime/script scan as the normal query path.
- New cases will have `forensic_all_dates`, an indexed multivalued `date` field with doc values. It contains the dates selected by the ingestion contract, which excludes `acquisition_time` and any multifields sourced from it. A matching timestamp from an eligible evidence field is still included. Original fields and `@timestamp` remain intact. Assume millisecond indexed precision for this integration; report any conflicting precision requirement.
- The ingestion pipeline owns array population. Verify its mapping and case capability signal where available; report missing prerequisites without inventing complete coverage or adding a silent fallback. No historical backfill is required. Old cases keep their existing behavior and are deleted when closed.

## Inspect the actual fork first

Locate and report the files/functions responsible for time state, table queries, histogram requests, ES compatibility translation, response parsing, plotting, interval selection and zoom. Identify whether aggregation filters use `query`, `post_filter`, `global` or separate requests. A table filter in `post_filter` alone does not constrain aggregations.

Public Kibana 2 is only a reference. Its `lib/query.rb` hardcodes a range on `@timestamp`; `DateHistogram` accepts a field but emits old `facets`. Its graph endpoint uses the default field, and `public/lib/js/ajax.js` reads facet entries and plots time/count pairs. Your fork already works with ES8, so find its actual adaptation before changing anything. Do not reinstall the historical API or assume these paths are unchanged.

Example request: `size: 0`, `aggs["0"].filter.bool.must` and `aggs["0"].aggs.dh.date_histogram`, with `dh.date_histogram.field` set to `forensic_all_dates` while TWO range clauses in the parent filter still target `@timestamp`. The interval is `fixed_interval: "30d"`. Preserve the existing request/response structure where practical; do not introduce a new aggregation adapter unless actual response parsing requires it. Trace the origin of both ranges and distinguish automatic time restrictions from intentional analyst field constraints. Verify the actual response and rendered chart.

Inspect a representative request and response when available, or establish their shape from code and fixtures. Distinguish code inspection, fixture tests and live verification in your report. If repository access is missing, state that blocker instead of claiming implementation.

## Implement the smallest coherent change

1. Add or reuse a temporal mode control: canonical `@timestamp` versus all included dates `forensic_all_dates`. Enable the latter only for cases with complete support. Pass the selected field through the existing query path; default to current behavior when the setting is absent.
2. In the new mode, replace the application's automatic `@timestamp` time range with ONE `range` on `forensic_all_dates`, containing both `gte: start` and `lt: end`. Preserve text, host, artifact and other analyst filters, including intentionally entered field constraints. Do not leave an implicit canonical time restriction in table, chart or adapter code.
3. Use native ES8 `date_histogram` on the selected field for the new mode. Apply the same time window and other filters to results and aggregation. Prefer the existing request structure; a histogram-only request should use `size: 0`. Reuse the current interval policy and cap the number of buckets. Do not replace all query builders or migrate unrelated chart modes.
4. Adapt `aggregations.<name>.buckets` minimally into the plotting contract already used by the fork: bucket key in epoch milliseconds and `doc_count`. If an adapter already performs this conversion, extend it there. Avoid changing the plotting library. Keep UTC instants and display timezone handling consistent; do not apply timezone offsets twice.
5. Label the new chart as documents with at least one timestamp per bucket. A document counts once within a bucket and can appear in multiple buckets. Do not calculate unique result totals by summing bars or label bars as timestamp/event counts. Historical facet counting is not a reliable contract for ES8.
6. Keep current table ordering and make its meaning visible. Sorting by the minimum array value does not mean sorting by the first timestamp that matched. Do not introduce that behavior or sort only the current page as if it were a global order. Keep the scalar timestamp column compatible; do not feed an array to its date formatter. Reuse the existing detail view to show original timestamp fields where practical.
7. Reuse the existing zoom/range-selection interaction. In the new mode it must regenerate the array range for both table and histogram and reset pagination as the application already does. Preserve mode and range through the application's existing URL/saved-search mechanism; verify existing reset/back behavior. Keep canonical mode and unsupported cases compatible.

## Handle partial histogram buckets correctly

A document-level range selects documents, not individual array values. `extended_bounds` generates empty buckets; it is not a filter. `hard_bounds` restricts bucket keys but cannot exclude out-of-window values inside a partial boundary bucket.

Implement or validate this indexed approach for exact counts:

- Resolve a consistent absolute window `[T0, T1)` per refresh. Use the same interval, timezone and offset rules for ES bucket rounding and the UI; preserve daylight-saving behavior for calendar intervals.
- Request the histogram for the visible bucket keys. Bounds must include the rounded first bucket key, even when it precedes T0, and must exclude any bucket starting at or after T1. Fill empty buckets consistently with the existing chart.
- Keep standard histogram counts for fully contained buckets. For each partial boundary bucket `[B0, B1)`, add a `filter` aggregation with ONE range on the selected field over `[max(B0, T0), min(B1, T1))`. It must share the histogram's other filters and selected document scope. Add at most two corrections; if both boundaries are in one bucket, use one.
- Replace each partial bucket's count with the corresponding filter `doc_count`, including zero. Do not add the correction to the original count. Preserve the bucket key and show the actual clipped interval in the tooltip where needed.
- If the panel also supports metrics or subaggregations, inspect their requirements. Do not silently apply corrected document counts to incompatible metrics; keep this change scoped to the count histogram unless supporting them is necessary and verified.

This boundary approach is a design proposal, not a claim that it is implemented or tested in our fork. Validate it with the focused cases below. If the existing adapter already provides equivalent behavior, reuse it.

## Acceptance checks

Use the existing test framework and a small isolated fixture; use local ES8 integration tests if available. Do not require a production cluster for basic validation. All times below are UTC on the same day unless stated otherwise.

| Case | Expected behavior |
|---|---|
| `@timestamp` outside the window, secondary date inside | Returned in all-dates mode; canonical mode retains its previous behavior. |
| Dates only before and after the window | No match. Both range bounds must apply to the same value. Test exact start inclusion and end exclusion too. |
| A has 10:10 and 10:20; B has 10:40 | The 10:00 hourly bucket counts 2 documents, not 3 timestamps. |
| One document has 10:10 and 11:10 | Counts once in each hourly bucket and once in results. |
| Window 09:45–11:15; A has 09:30 and 10:10; B has 10:20 and 11:30 | Corrected bucket counts are 09:00 = 0, 10:00 = 2, 11:00 = 0. Both documents match. |
| Window 10:15–10:45 within one hourly bucket | Apply one correction; only documents with a value in this exact interval count. Also test an empty result and a window aligned to bucket boundaries. |
| Zoom, other filters and a later source index in the same case | Chart and table use the same mode, filters and alias; new source coverage is preserved. |
| Old case, unset mode, timezone/DST change, timeout or shard failure | Existing behavior remains available; time boundaries remain correct; incomplete responses are not presented as complete results. |

Report changed files, the actual request/response shape, tests run, tests unavailable, ingestion prerequisites and any remaining fork limitations. Do not claim scale validation from a small fixture. Keep the patch focused on the count histogram, its temporal query and necessary state propagation.

## Primary references

- [ES8 date histogram](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-datehistogram-aggregation.html)
- [ES8 filter aggregation](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-filter-aggregation.html)
- [Historical Kibana 2 query implementation](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/lib/query.rb)
- [Historical Kibana 2 response and rendering code](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/public/lib/js/ajax.js)
