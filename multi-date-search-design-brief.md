# Multi date search and histogram design

Add one indexed array of dates to each evidence document so investigators can find it when **any included timestamp falls within the selected interval**. Use the same field for the histogram. This proposal covers the array approach only; the accompanying [GPT 5.6 Terra prompt](./gpt-5.6-terra-kibana2-implementation-prompt.md) describes how to inspect and adapt the existing application.

## Assumptions and scope

- Velociraptor offline collector ZIP files contain JSONL evidence, parsed into Elasticsearch 8. The exact minor version is unknown; this design does not require a minor upgrade as a prerequisite.
- Each case has one index per artifact source. An existing case alias includes all its source indices; there are no indices shared between cases.
- Cases may contain approximately 1,000 hosts and gigabytes of evidence per host. Actual document counts, timestamp counts and cluster capacity remain unmeasured.
- The feature applies only to new cases, from their first import onward. No historical migration is required. Closing a case deletes its data.
- The existing `@timestamp` remains available for the current search mode. The new mode selects documents, with one result row per document.
- The frontend is a custom Kibana 2 fork. Public upstream code was inspected, but the actual fork and its Elasticsearch adapter were not available. Examples below have not been executed against your system.

## The new field

The parser populates `forensic_all_dates` with normalized timestamps from a versioned catalog of source fields. Use one field definition across every source index in a new case, including indices created by later imports. Elasticsearch supports multiple values in a `date` field without a separate array type. [Elasticsearch arrays](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/array.html)

Proposed mapping, assuming millisecond search precision is acceptable:

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

For an MFT document whose creation and modification dates differ:

```json
{
  "@timestamp": "2026-01-12T09:30:00Z",
  "Created0x10": "2026-01-12T09:30:00Z",
  "LastModified0x10": "2026-09-20T16:45:00Z",
  "forensic_all_dates": [
    "2026-01-12T09:30:00Z",
    "2026-09-20T16:45:00Z"
  ]
}
```

The September search returns this document even though its canonical timestamp is in January. Identical timestamps may be deduplicated in the derived array. Preserve original fields to explain which timestamp matched; the array alone does not retain field names.

Send the following temporal clause inside the existing query's `bool.filter` to the **existing case alias**, alongside the other active filters:

```json
{
  "range": {
    "forensic_all_dates": {
      "gte": "2026-09-20T00:00:00Z",
      "lt": "2026-09-21T00:00:00Z"
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
| **3. Meaning and coverage of all dates** | **Active.** Define the catalog from artifact schemas and mappings, including nested source values where applicable. Literal “all date fields” includes acquisition/ingestion dates if present. Label any curated subset accurately. Date-like strings or numbers need explicit interpretation; recursive string guessing is insufficient. |
| **4. Search, histogram and ordering** | **Active.** A matching document appears once in results but can contribute to multiple bars. An array sort does not automatically choose the date that matched. Keep the current ordering clearly labelled; explain matches using original fields. |
| **5. Precision and invalid values** | **Active.** Normalize units and timezone, preserve source evidence, and report invalid or excluded values. A `date` array uses milliseconds; copying `date_nanos` into it loses finer precision. Confirm that tradeoff before release. `date_nanos` has a narrower supported date range and still uses millisecond aggregation resolution. [Date types](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/date.html), [date_nanos](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/date_nanos.html) |
| **6. Correct interval matching** | **Active.** Put both bounds in one range, using `[start, end)`. Separate lower/upper range clauses may match different array values. Do not substitute an interval between the array minimum and maximum: values only in January and December must not match June. |

## Histogram behavior in Kibana 2

Use Elasticsearch 8 `date_histogram` on `forensic_all_dates`. Each bar means **documents with at least one included timestamp in that period**. Two dates from one document in the same bucket count once; that document may count again in another bucket. The sum of bars is therefore not the number of unique results. [ES8 histogram implementation](https://github.com/elastic/elasticsearch/blob/v8.19.0/server/src/main/java/org/elasticsearch/search/aggregations/bucket/histogram/DateHistogramAggregator.java)

The public Kibana 2 code hardcodes the base time filter to `@timestamp`, builds an old `facets` request and renders returned time/count pairs. The renderer does not inspect document arrays. The proposed integration is to parameterize the temporal field and adapt modern aggregation buckets to the renderer's existing input, wherever the fork's current adapter makes this simplest. Upstream behavior is a clue, not proof of how your fork works. [Historical query code](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/lib/query.rb#L39-L163), [historical renderer](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/public/lib/js/ajax.js#L1313-L1351)

**Development evidence from 2 October 2026:** a supplied request shows that the fork already uses native `aggs`, and changing the histogram setting selects `forensic_all_dates`. However, both parent range filters remain on `@timestamp`. The immediate integration work is therefore to trace and adapt the temporal filters while preserving the existing aggregation structure. The screenshot does not establish the response or rendering behavior. [Read-only comparison requests](./poc/histogram-from-observed-query.http)

Two details require explicit verification:

- **Count semantics:** historical facets counted timestamp values; ES8 `doc_count` counts documents per bucket. Label the new mode accordingly and verify the fork's actual request and response. [Historical facet implementation](https://github.com/elastic/elasticsearch/blob/v0.90.13/src/main/java/org/elasticsearch/search/facet/datehistogram/CountDateHistogramFacetExecutor.java)
- **Zoom boundaries:** filtering documents does not remove their other array values. For a 09:45–11:15 window, a document with 09:30 and 10:10 matches but must not inflate the partial 09:45–10:00 bar. Bounds alone do not solve this. Proposed correction: retain the histogram for full buckets and replace up to two partial boundary counts with indexed range `filter` aggregations. Verify this in the fork; the implementation prompt specifies the algorithm. [Histogram bounds](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-datehistogram-aggregation.html), [filter aggregation](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-filter-aggregation.html)

Keep the existing plotting library, interval selection and zoom interaction where possible. Both table and chart must use the same case alias, temporal mode, window and other filters. Preserve current canonical mode behavior.

## Cost and release checks

This adds timestamp values to existing documents, without additional indices or duplicate evidence documents. For N documents and k distinct dates on average, the field adds approximately N × k stored/indexed values; actual bytes depend on compression, doc values, `_source` and replicas. Ingestion costs increase. A single indexed field simplifies searches, but neither latency nor storage savings are guaranteed, and the case still spans the same shards.

Measure import throughput, index size, query and histogram p95 latency, CPU/heap and concurrent usage on representative MFT and log data. Bound the number of chart buckets, aggregate on the server, and surface timeouts or partial shard failures. Release after checking secondary-date matches, interval boundaries, per-bucket counts, partial zoom buckets, later source imports and unchanged canonical mode. Actual fork effort cannot be estimated reliably until its query and response paths are inspected.
