# Multi date search and histogram design

Add one indexed array of dates to each evidence document so investigators can find it when **any included timestamp falls within the selected interval**. Use the same field for the histogram. This proposal covers the array approach only; the accompanying [GPT 5.6 Terra prompt](./gpt-5.6-terra-kibana2-implementation-prompt.md) describes how to inspect and adapt the existing application.

## Assumptions and scope

- Velociraptor offline collector ZIP files contain JSONL evidence, parsed into Elasticsearch 8. The exact minor version is unknown; this design does not require a minor upgrade as a prerequisite.
- Each case has one index per artifact source. An existing case alias includes all its source indices; there are no indices shared between cases.
- Cases may contain approximately 1,000 hosts and gigabytes of evidence per host. Actual document counts, timestamp counts and cluster capacity remain unmeasured.
- The feature applies only to new cases, from their first import onward. No historical migration is required. Closing a case deletes its data.
- The existing `@timestamp` remains available for the current search mode. The new mode selects documents, with one result row per document.
- The frontend is a custom Kibana 2 fork. Public upstream code was inspected, but the actual fork and its Elasticsearch adapter were not available in this checkout. The dashboard configurations below are examples; full application and cluster validation remain required.

## The new field

The parser populates `forensic_all_dates` with dates from a versioned catalog of source fields. Preserve the original ISO date strings so analysts can compare them directly with the evidence fields; the new field indexes them at millisecond precision. Use one field definition across every source index in a new case, including indices created by later imports. Elasticsearch supports multiple values in a `date` field without a separate array type. [Elasticsearch arrays](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/array.html)

Mapping addition for the MFT index used in the `ScheduledDefrag` example below, assuming millisecond search precision is acceptable. Existing mappings for `@timestamp`, `mft.fn_atime`, `mft.fn_btime`, `mft.fn_ctime`, `mft.fn_mtime`, `mft.si_atime`, `mft.si_ctime` and `mft.si_mtime` remain in place:

```json
{
  "properties": {
    "forensic_all_dates": {
      "type": "date",
      "format": "strict_date_optional_time||epoch_millis"
    }
  }
}
```

Example: **`ScheduledDefrag`**, MFT record **227611**. Dotted field names are used for readability. The `forensic_all_dates` array preserves the original strings and excludes `acquisition_time`:

```json
{
  "artifact_source": "mft",
  "file.path": "ScheduledDefrag",
  "fullpath": ".\\Windows\\System32\\Tasks\\Microsoft\\Windows\\Defrag",
  "mft.id": 227611,
  "@timestamp": "2026-03-24T01:09:47.8609249+00:00",
  "mft.fn_atime": "2026-03-23T18:23:55.4739326+00:00",
  "mft.fn_btime": "2026-03-24T01:09:47.8609249+00:00",
  "mft.fn_ctime": "2026-03-23T18:23:55.4739326+00:00",
  "mft.fn_mtime": "2026-03-23T18:23:55.4739326+00:00",
  "mft.si_atime": "2026-07-02T18:41:34.1708833+00:00",
  "mft.si_ctime": "2026-04-08T15:32:59.2831915+00:00",
  "mft.si_mtime": "2026-04-08T15:32:59.2831915+00:00",
  "acquisition_time": "2026-07-02T18:40:39Z",
  "forensic_all_dates": [
    "2026-03-24T01:09:47.8609249+00:00",
    "2026-03-23T18:23:55.4739326+00:00",
    "2026-07-02T18:41:34.1708833+00:00",
    "2026-04-08T15:32:59.2831915+00:00"
  ]
}
```

Several fields share the same value: `@timestamp` and `mft.fn_btime` contain the 24 March date; `mft.fn_atime`, `mft.fn_ctime` and `mft.fn_mtime` contain the 23 March date; `mft.si_ctime` and `mft.si_mtime` contain the 8 April date. Removing identical strings leaves **four distinct dates**. `acquisition_time` remains in the evidence document but is not copied into the array.

A search for **8 April 2026** returns this document because `mft.si_ctime` and `mft.si_mtime` fall on that day, even though its canonical `@timestamp` is **24 March 2026**. Preserve the original fields to explain which timestamp matched; the array alone does not retain field names.

To search that day in UTC, send the following temporal clause inside the existing query's `bool.filter` to the **existing case alias**, alongside the other active filters:

```json
{
  "range": {
    "forensic_all_dates": {
      "gte": "2026-04-08T00:00:00Z",
      "lt": "2026-04-09T00:00:00Z"
    }
  }
}
```

Version the catalog and record feature availability in case metadata. Enable the mode only for cases whose imports consistently populate the field. Do not infer complete coverage from finding the field in one index.

## Which of the six concerns still apply

| Original point | Status and required behavior |
|---|---|
| **1. Existing time filter** | **Active.** In the new mode, replace the automatic `@timestamp` range with the array range. Keeping both in AND loses documents with only secondary dates inside the window. Preserve other analyst filters. |
| **2. Searching every source** | **Already addressed by the case alias.** Reuse it for both results and histogram. Ensure later source indices continue to join it; no new index grouping mechanism is needed. |
| **3. Meaning and coverage of all dates** | **Active.** Define the catalog from artifact schemas and mappings, including nested source values where applicable. Exclude `acquisition_time` and any multifields sourced from it: acquisition metadata is outside the agreed forensic date catalog. Retain a matching timestamp if it also occurs in an eligible evidence field. Label the catalog and any further subset accurately. Date-like strings or numbers need explicit interpretation; recursive string guessing is insufficient. |
| **4. Search, histogram and ordering** | **Active.** A matching document appears once in results but can contribute to multiple bars. An array sort does not automatically choose the date that matched. Keep the current ordering clearly labelled; explain matches using original fields. |
| **5. Precision and invalid values** | **Active.** Normalize units and timezone, preserve source evidence, and report invalid or excluded values. A `date` array uses milliseconds; copying `date_nanos` into it loses finer precision. Confirm that tradeoff before release. `date_nanos` has a narrower supported date range and still uses millisecond aggregation resolution. [Date types](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/date.html), [date_nanos](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/date_nanos.html) |
| **6. Correct interval matching** | **Active.** Put both bounds in one range, using `[start, end)`. Separate lower/upper range clauses may match different array values. Do not substitute an interval between the array minimum and maximum: values only in January and December must not match June. |

## Configuring the dashboard for multiple dates

This example uses a separate dashboard, **`ir-multidate-swords.json`**, based on **`ir-data-explorer-swords.json`**. The comparison below shows the settings for the original and multi-date configurations.

**The example configuration points the time filter, histogram, results table and time selector at `forensic_all_dates`.** Populating the field in Elasticsearch does not update these dashboard settings automatically. Changing only the histogram field can leave the search selecting documents by their original `@timestamp`.

| Dashboard setting | Original configuration | Multi-date configuration | Purpose |
|---|---|---|---|
| Time filter: `services.filter.list["0"].field` | `@timestamp` | `forensic_all_dates` | Select documents when an included forensic date falls in the chosen period. |
| Histogram panel: `time_field` | `@timestamp` | `forensic_all_dates` | Build the bars from the included forensic dates. |
| Results table: `timeField` | `@timestamp` | `forensic_all_dates` | Point the table's time-field setting at the same field as the rest of the dashboard. |
| Results table: `sort` | `["@timestamp", "desc"]` | `["forensic_all_dates", "desc"]` | Change the field used for descending result order. This does not guarantee ordering by the particular date that matched the filter. |
| Time selector: root `nav` section | No `nav` section in this example | Added a `timepicker` using `forensic_all_dates` | Make changes to the selected period use the forensic date field as well. |

In this example, the histogram is `rows[0].panels[0]` and the results table is `rows[1].panels[0]`. The setting names differ: **`time_field`** for the histogram, **`timeField`** for the table and **`timefield`** for the time selector. Preserve that spelling when editing the JSON.

The added time selector sits in the root `nav` array. Its relevant settings are:

```json
{
  "type": "timepicker",
  "timefield": "forensic_all_dates",
  "filter_id": 0,
  "now": true,
  "enable": true
}
```

This example shows the relevant control settings. `filter_id: 0` links the selector to the time filter listed above. Keep the predefined time ranges, refresh intervals and existing start/end arguments, with “last 24 hours” and “now” as their defaults.

The histogram remains in **count** mode, with **automatic interval selection** and the **browser timezone**. The configured `5m` interval is therefore not a promise that every view uses five-minute bars: a broad period can use 30-day bars, as in the example below. This is why the reading guidance below matters even after the field settings have been changed.

After loading the multi-date dashboard, test a document whose creation and modification dates differ. Select a period containing only a secondary date and check that the document appears, then change the period and check both the table and chart. Verify that the generated query uses `forensic_all_dates` for both automatic time ranges and the histogram. Preserve any separate date constraints deliberately added by the analyst.

## Reading the histogram

Each bar represents a period of time and shows **how many documents matching the active search filters have at least one included date in that period**. Use the histogram to find periods of activity, then open the matching documents to inspect their original date fields and understand what happened.

- **The date on a bar marks the start of its period.** It does not mean an event occurred at that exact moment. A bar can group dates from several days, depending on the interval selected in the chart.
- **The height counts documents, not individual dates.** A document with two dates in the same period contributes **1**, not 2, to that bar. The same document can contribute to another bar if it also has a date in another period.
- **The results table counts each matching document once.** Adding the bar heights can therefore give a larger number than the number of search results. A chart total labelled “hits” must not be read as a count of unique documents if it is obtained by adding the bars.

### Example: one document, four dates, three bars

The same `ScheduledDefrag` document has included dates on **23 March, 24 March, 8 April and 2 July 2026**. With a **30-day interval**, it produces the following three bars:

| Bar label in the Italian timezone | Document dates grouped into that bar (UTC) | Bar height |
|---|---|---|
| 8 March 2026, 01:00 | 23 March and 24 March | 1 document |
| 7 April 2026, 02:00 | 8 April | 1 document |
| 6 June 2026, 02:00 | 2 July | 1 document |

All four dates are represented. The two March dates share a bar. The July date belongs to the 30-day period starting on 6 June, so **a June label can correctly represent a July date**. These are fixed 30-day periods, not calendar months.

In this example, the periods start at midnight UTC. An Italian display shows that as 01:00 in winter time and 02:00 in summer time; the underlying instants have not changed. The result is **one document in the table, four included dates and three bars of height 1**. [Date histogram intervals and timezones](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-datehistogram-aggregation.html)

Choose a smaller interval to see more detail: a daily interval would separate the two March dates and produce **four bars** for this example. Changing the interval changes how dates are grouped, not the underlying evidence.

Only dates in the agreed catalog contribute to this view; **`acquisition_time` is excluded**. The histogram does not identify whether a date means creation, modification or access. Check the original fields in the document for that meaning.

## Histogram implementation in Kibana 2

Use Elasticsearch 8 `date_histogram` on `forensic_all_dates`. Each bar means **documents with at least one included timestamp in that period**. Two dates from one document in the same bucket count once; that document may count again in another bucket. The sum of bars is therefore not the number of unique results. [ES8 histogram implementation](https://github.com/elastic/elasticsearch/blob/v8.19.0/server/src/main/java/org/elasticsearch/search/aggregations/bucket/histogram/DateHistogramAggregator.java)

The public Kibana 2 code hardcodes the base time filter to `@timestamp`, builds an old `facets` request and renders returned time/count pairs. The renderer does not inspect document arrays. The proposed integration is to parameterize the temporal field and adapt modern aggregation buckets to the renderer's existing input, wherever the fork's current adapter makes this simplest. Upstream behavior is a clue, not proof of how your fork works. [Historical query code](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/lib/query.rb#L39-L163), [historical renderer](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/public/lib/js/ajax.js#L1313-L1351)

**Example:** a request uses native `aggs` with the histogram field set to `forensic_all_dates`, while both parent range filters still use `@timestamp`. The dashboard configuration above aligns the automatic time ranges and histogram on `forensic_all_dates`. Preserve the existing aggregation structure and verify the remaining behavior below. [Read-only comparison examples](./poc/histogram-from-observed-query.http)

Two details require explicit verification:

- **Count semantics:** historical facets counted timestamp values; ES8 `doc_count` counts documents per bucket. Label the new mode accordingly and verify the fork's actual request and response. [Historical facet implementation](https://github.com/elastic/elasticsearch/blob/v0.90.13/src/main/java/org/elasticsearch/search/facet/datehistogram/CountDateHistogramFacetExecutor.java)
- **Zoom boundaries:** filtering documents does not remove their other array values. For a 09:45–11:15 window, a document with 09:30 and 10:10 matches but must not inflate the partial 09:45–10:00 bar. Bounds alone do not solve this. Proposed correction: retain the histogram for full buckets and replace up to two partial boundary counts with indexed range `filter` aggregations. Verify this in the fork; the implementation prompt specifies the algorithm. [Histogram bounds](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-datehistogram-aggregation.html), [filter aggregation](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-filter-aggregation.html)

Keep the existing plotting library, interval selection and zoom interaction where possible. Both table and chart must use the same case alias, temporal mode, window and other filters. Preserve current canonical mode behavior.

## Cost and release checks

This adds timestamp values to existing documents, without additional indices or duplicate evidence documents. For N documents and k distinct dates on average, the field adds approximately N × k stored/indexed values; actual bytes depend on compression, doc values, `_source` and replicas. Ingestion costs increase. A single indexed field simplifies searches, but neither latency nor storage savings are guaranteed, and the case still spans the same shards.

Measure import throughput, index size, query and histogram p95 latency, CPU/heap and concurrent usage on representative MFT and log data. Bound the number of chart buckets, aggregate on the server, and surface timeouts or partial shard failures. Release after checking secondary-date matches, interval boundaries, per-bucket counts, partial zoom buckets, later source imports and unchanged canonical mode. Actual fork effort cannot be estimated reliably until its query and response paths are inspected.
