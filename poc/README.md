# Try the date array on an existing development index

This proof of concept adds `forensic_all_dates` to existing Elasticsearch 8 documents. It does not require a parser change, template rollout or Kibana implementation. It targets **one concrete artifact source index**, as requested; it does not update every index behind the case alias.

Use the Python script to populate the field, then run the supplied searches to check matching and histogram counts. This verifies the Elasticsearch behavior. Whether the existing Kibana 2 panel can display it without changes still depends on the fork's query and response adapter.

## Run it

Requirements: Python 3, access to the development Elasticsearch HTTP endpoint, and an account able to read the mapping/documents, add a field mapping, update documents and refresh this index. The script uses the standard library; no packages need to be installed.

From this directory, set the endpoint and the **concrete index name**, replacing these examples:

```bash
export ES_URL='https://your-development-elasticsearch:9200'
export POC_INDEX='case-123-windows-ntfs-mft'
```

If authentication is required, set `ES_API_KEY` to the encoded Elasticsearch API key, or set both `ES_USERNAME` and `ES_PASSWORD`. For a private certificate authority, set `ES_CA_CERT` to its PEM file path. The script verifies TLS and does not print credentials. Set credentials through your normal environment/secret mechanism; do not put them in the script.

Preview discovery and up to 1,000 documents, without changing mappings or documents:

```bash
python3 backfill_dates.py --index "$POC_INDEX"
```

For your development environment with few documents, populate **all documents that do not already have the target field**:

```bash
python3 backfill_dates.py --index "$POC_INDEX" --max-docs 0 --apply
```

For a smaller initial write, replace `0` with `100`. The limit is the number of candidate documents examined, including those skipped because they have no usable dates. Selection follows shard order; it is not a random or representative sample.

If you want a POC limited to known fields:

```bash
python3 backfill_dates.py --index "$POC_INDEX" \
  --fields '@timestamp,Created0x10,LastModified0x10' --max-docs 0 --apply
```

Use names from your actual mapping. An explicit field list means partial coverage. If the target name is already used for another purpose, choose `--target forensic_all_dates_poc` and replace the field name in the verification queries too. Existing target values are not overwritten; to rerun with a different source catalog, use a fresh target name.

## What the script does

1. Reads the mapping and discovers physical `date` and `date_nanos` fields, including date fields inside ordinary objects and multifields.
2. Requests their doc values formatted by Elasticsearch as `epoch_millis`. This avoids confusing epoch seconds with milliseconds or interpreting custom source date formats in Python. It copies dates that are already searchable, not malformed raw values.
3. Adds the target mapping as an indexed `date` with doc values, if absent.
4. Iterates a point-in-time view in small pages, combines each document's dates, removes duplicates and writes only the new field using partial bulk updates. Original evidence fields and `@timestamp` are retained. Concurrency checks prevent a stale calculation from overwriting a document changed since it was read.
5. Reports examined, prepared, updated, skipped and failed counts. Each batch is refreshed so a **new search** can see the results. This refresh policy is convenient for a small development trial, not a bulk production import strategy.

The script creates a stored and indexed field; it is not a virtual field calculated at query time. Updates reindex the affected documents internally. It skips documents with existing target values or without usable dates. It does not populate future imports, update index templates, mark the case as fully supported, or configure the Kibana time selector.

The preview opens and closes a temporary point-in-time search context but performs no mapping or document writes. Applying the change is not transactional: if a batch fails, successful updates and any new mapping remain. Errors stop execution and produce a nonzero exit code. A rerun reads fresh data and skips documents already populated; transport failures can leave the last batch's outcome uncertain.

## Verify the proposal

Open [verify-array.http](./verify-array.http). Replace `POC_INDEX` with the tested index and replace the example dates with a period represented in your data. Run the requests in Kibana Dev Tools if the fork provides a Console, or send the same HTTP requests directly to Elasticsearch.

The file checks coverage, displays sample arrays, finds documents whose secondary dates match while `@timestamp` does not, and calculates the count histogram. It also demonstrates the two corrected counts needed for partial boundary buckets. Elasticsearch returns those corrections separately; the existing chart will not merge them automatically.

For a useful test, pick an MFT record whose creation and modification dates differ. Choose a window containing only the secondary date. The array query should return the record; a range on its canonical `@timestamp` alone should not. If only a sample was populated, compare behavior only within documents that have the new field, not against the whole index or whole case.

If the panel already lets you choose its temporal field, try `forensic_all_dates`. Check the actual network request: both the filter and histogram must use it. Merely adding the mapping does not change a hardcoded `@timestamp` filter. Keep any field refresh limited to what your fork already supports; this POC does not presume a modern Kibana data-view interface.

## Deliberate limits

- `date_nanos` values are rounded down to milliseconds in the new field; their originals remain unchanged.
- Nested dates and dates with disabled doc values cause an explicit error, rather than silently incomplete coverage. Use an explicit supported subset for a partial test. Runtime dates, unmapped date-like values and malformed values excluded from indexing are not copied. Mapping-level `_source` pruning or disabled `_source` is unsupported.
- At most 100 source fields are selected, matching the default doc-value retrieval limit. A lower custom index limit can still reject the request; select fewer fields instead of changing cluster settings for this test.
- Newly added field mappings cannot simply be removed from an existing index. Removing values later would not remove their mapping. This tool does not delete data or provide an automatic rollback.
- The local checks use a simulated ES client to validate payloads, normalization, pagination and failure handling. No live Elasticsearch cluster or your Kibana fork was available for execution; first run the preview in your development environment.

Primary references: [formatted doc values](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-fields.html#docvalue-fields), [partial updates](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-update.html), [bulk requests](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-bulk.html), [point in time](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/point-in-time-api.html).
