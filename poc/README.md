# Try the date array on an existing development index

This proof of concept adds `forensic_all_dates` to existing Elasticsearch 8 documents. It does not require a parser change, template rollout or Kibana implementation. It targets **one concrete artifact source index**, as requested; it does not update every index behind the case alias.

Use the Python script to populate the field, then run the supplied searches to check matching and histogram counts. The array now preserves the original ISO date strings from `_source`, including fractional digits and timezone spelling, so an analyst can compare its values directly with the original fields. The root field `acquisition_time` is always excluded: acquisition metadata must not contribute dates to the forensic timeline. Check the display on the old Kibana as part of the trial; the local tests do not verify its renderer.

## Run it

Requirements: Python 3, access to the development Elasticsearch HTTP endpoint, and an account able to read the mapping/settings/documents, add a field mapping, update documents and refresh this index. The script uses the standard library; no packages need to be installed.

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

Populate **all documents that do not already have the target field**:

```bash
python3 backfill_dates.py --index "$POC_INDEX" --max-docs 0 --apply
```

For a smaller initial write, replace `0` with `100`. The limit is the number of candidate documents examined, including those skipped because they have no usable dates. Selection follows shard order; it is not a random or representative sample.

If you want a POC limited to known fields:

```bash
python3 backfill_dates.py --index "$POC_INDEX" \
  --fields '@timestamp,Created0x10,LastModified0x10' --max-docs 0 --apply
```

Use names from your actual mapping. An explicit field list means partial coverage. Requesting `acquisition_time` or a date multifield sourced from it through `--fields` produces an error; the exclusion cannot be overridden. If the target name is already used for another purpose, choose `--target forensic_all_dates_poc` and replace the field name in the verification queries too. Existing target values are not overwritten; to rerun with a different source catalog, use a fresh target name.

To test the new string representation and acquisition-time exclusion, point `POC_INDEX` at another concrete development index whose documents have not yet been populated, then run the preview before applying it. This version neither converts numeric arrays nor removes acquisition dates from arrays written by an earlier run: those documents still have an existing target and are skipped. A fresh `--target` name is an alternative for testing on the same index.

## Larger backfills

The defaults use pages of 1,000 documents and `refresh=false` on each bulk, followed by one explicit refresh on exit if any bulk was attempted. For two million eligible documents, this means approximately 2,000 search/bulk pairs rather than 10,000 pairs with the old 200-document default. It also removes the old forced refresh after every bulk. Normal automatic index refreshes still run; the script does not change index settings or replicas.

Start with a bounded write to measure the actual cluster, then continue with all remaining candidates:

```bash
python3 backfill_dates.py --index "$POC_INDEX" \
  --max-docs 20000 --batch-size 1000 --refresh final --apply

python3 backfill_dates.py --index "$POC_INDEX" \
  --max-docs 0 --batch-size 1000 --refresh final --apply
```

The first command changes up to 20,000 documents; it is not a preview. Already populated documents are skipped by the next run. Progress is printed after a completed batch at most once every 10 seconds, with cumulative counts, elapsed seconds, `scanned_per_second` and `updated_per_second`. The final summary includes cleanup and refresh time. Use `--progress-every 30` to reduce logging or `--progress-every 0` for only the final summary. A slow individual request can delay progress output.

Use confirmed `updated_per_second` to estimate remaining time only when the trial is representative. For example, 1,000 updates/s would imply about 33 minutes for two million updates; this is arithmetic, not a measured throughput claim. Document size, dates per document, shards, concurrent traffic and sustained merge pressure can change the rate. If batches are too heavy, compare `--batch-size 200`, `500` and `1000` on comparable data.

`--refresh batch` restores the previous immediate visibility after each bulk. `--refresh none` requests no explicit refresh; new searches see updates according to the index refresh policy. The default final refresh is also attempted after a failed bulk so successful writes can become searchable. A failed final refresh produces a nonzero exit code; if an earlier error exists, it remains the primary error. A refresh cannot resolve an ambiguous write outcome or roll back successful writes.

Requests remain sequential and the standard-library HTTP client does not pool persistent connections. If throughput is still insufficient, measure request latency and cluster CPU/I/O before adding connection pooling and a bounded number of concurrent bulk workers. This version does not automatically retry failed updates or version conflicts, since stale date arrays must not overwrite newer data. Bulk sizing, concurrency and refresh guidance: [Elastic indexing performance](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/tune-for-indexing-speed.html), [refresh behavior](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-refresh.html).

## What the script does

1. Reads the mapping and source settings, and discovers physical `date` and `date_nanos` fields, including date fields inside ordinary objects and multifields. A multifield reads the raw value from its parent field in `_source`. The root `acquisition_time` field and date multifields sourced from it are excluded. Other source fields remain eligible; a matching date is still copied if it occurs in an allowed field.
2. Adds the target mapping as an indexed `date` with doc values, if absent and `--apply` is set.
3. Reads original ISO strings from `_source` and requests doc values formatted by Elasticsearch as `epoch_millis` for validation. Fields without indexed values are skipped. For each field with indexed values, the source strings must represent exactly the same set of indexed milliseconds. Unsupported values or a mismatch stop the current page before its bulk is sent; the script does not silently convert values or copy dates with changed meaning.
4. Iterates a point-in-time view in bounded pages, combines each document's dates, removes identical strings and writes only the new field using partial bulk updates. Different strings representing the same instant are retained so each original representation remains available for comparison. Original evidence fields and `@timestamp` are retained. Concurrency checks prevent a stale calculation from overwriting a document changed since it was read.
5. Reports examined, prepared, updated, skipped and failed counts plus elapsed time and throughput. By default it refreshes once after closing the point-in-time view, so a **new search** can see the results.

The script creates a stored and indexed field; it is not a virtual field calculated at query time. Updates reindex the affected documents internally. It skips documents with existing target values or without usable dates. It does not populate future imports, update index templates, mark the case as fully supported, or configure the Kibana time selector.

For example, the source value `2026-03-24T01:09:47.8609249+00:00` is copied verbatim into `forensic_all_dates`, rather than written as `1774314587860` or reformatted as `2026-03-24T01:09:47.860Z`. The target remains an indexed `date` with `strict_date_optional_time||epoch_millis`; its search precision is still milliseconds.

The preview opens and closes a temporary point-in-time search context but performs no mapping or document writes. Applying the change is not transactional: if a batch fails, successful updates and any new mapping remain. Errors stop execution and produce a nonzero exit code. A rerun reads fresh data and skips documents already populated; transport failures can leave the last batch's outcome uncertain.

## Verify the proposal

The example in [histogram-from-observed-query.http](./histogram-from-observed-query.http) uses the `0` / `dh` aggregation structure. It compares canonical filters with an array filter and lists secondary-date matches omitted by the canonical selection. In this example, both automatic ranges still use `@timestamp`, although the histogram field has changed. Identify their origin in the fork before changing them; intentionally entered analyst constraints should not be removed automatically.

Open [verify-array.http](./verify-array.http). Replace `POC_INDEX` with the tested index and replace the example dates with a period represented in your data. Run the requests in Kibana Dev Tools if the fork provides a Console, or send the same HTTP requests directly to Elasticsearch.

The file checks coverage, displays sample arrays, finds documents whose secondary dates match while `@timestamp` does not, and calculates the count histogram. It also demonstrates the two corrected counts needed for partial boundary buckets. Elasticsearch returns those corrections separately; the existing chart will not merge them automatically.

For a useful test, pick an MFT record whose creation and modification dates differ. Choose a window containing only the secondary date. The array query should return the record; a range on its canonical `@timestamp` alone should not. If only a sample was populated, compare behavior only within documents that have the new field, not against the whole index or whole case.

If the panel already lets you choose its temporal field, try `forensic_all_dates`. Check the actual network request: both the filter and histogram must use it. Merely adding the mapping does not change a hardcoded `@timestamp` filter. Keep any field refresh limited to what your fork already supports; this POC does not presume a modern Kibana data-view interface.

## Deliberate limits

- For fields with indexed values, source values must be ISO strings in `YYYY-MM-DD` or `YYYY-MM-DDTHH:mm:ss` form, optionally with 1–9 fractional digits and a `Z` or `±HH:mm` timezone. A missing timezone is interpreted as UTC for validation; the copied string is unchanged. Numeric epochs and other source date formats are rejected instead of reformatted. A custom source mapping is usable only when its actual strings fit this supported syntax and their indexed milliseconds agree.
- `date_nanos` strings retain all original fractional digits in the target's `_source`, but the target indexes their instants at millisecond precision. Distinct source strings can therefore become the same indexed date value.
- Nested dates and dates with disabled doc values cause an explicit error, rather than silently incomplete coverage. Use an explicit supported subset for a partial test. Runtime dates and unmapped date-like values are not copied. Ignored or substituted values must not cause the copied array to disagree with the indexed values; unsupported source values or a source/doc-values mismatch stop the page. Mapping-level `_source` pruning, disabled `_source`, and synthetic `_source` are unsupported because exact original values must be available.
- An existing target must have a compatible indexed `date` mapping with `strict_date_optional_time` as its first format. The script does not change an existing mapping or overwrite an existing target, including an empty or null target in `_source`.
- At most 100 source fields are selected, matching the default doc-value retrieval limit. A lower custom index limit can still reject the request; select fewer fields instead of changing cluster settings for this test.
- Newly added field mappings cannot simply be removed from an existing index. Removing values later would not remove their mapping. This tool does not delete data or provide an automatic rollback.
- The local checks use a simulated ES client to validate source-string preservation, indexed-date validation, payloads, pagination and failure handling. No live Elasticsearch cluster or your Kibana fork was available for execution; first run the preview in your development environment.

Primary references: [formatted doc values](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-fields.html#docvalue-fields), [partial updates](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-update.html), [bulk requests](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-bulk.html), [point in time](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/point-in-time-api.html).

## Discover date fields for artifact configuration files

`discover_artifact_dates.py` builds the `forensic_all_dates` field catalog in local artifact YAML files. It reads Elasticsearch mappings and, where necessary, original document values from one nonempty representative index per artifact. It makes no document, mapping or index-setting changes. It saves local discovery checkpoints; `--apply` additionally writes eligible local YAML files, without backups or automatic Git operations.

### Install and run

The discovery script requires Python 3.9 or newer and `ruamel.yaml`. From the repository root, install its dependency in your existing virtual environment, or create a dedicated one:

```bash
python3 -m venv .venv-discovery
. .venv-discovery/bin/activate
python -m pip install -r poc/requirements-discovery.txt
```

Set the Elasticsearch endpoint and the existing artifact configuration directory:

```bash
export ES_URL='https://your-development-elasticsearch:9200'
export ARTIFACTS_DIR='/path/to/collections_processor/src/processor/conf/velociraptor/artifacts'
```

Authentication follows the backfill script: set `ES_API_KEY` to an encoded Elasticsearch API key, or set both `ES_USERNAME` and `ES_PASSWORD`. For a private certificate authority, set `ES_CA_CERT` to its PEM file path. Supply credentials through your normal environment or secret mechanism. The account needs permission to resolve indices, read mappings/settings, search documents, and open/close point-in-time search contexts.

Preview the proposed YAML differences without writing YAML files (a discovery checkpoint is saved):

```bash
python poc/discover_artifact_dates.py
```

Apply the completed preview using its saved discovery, or start a new scan with optional index and directory overrides:

```bash
python poc/discover_artifact_dates.py --resume --apply

python poc/discover_artifact_dates.py \
  --index-pattern 'ir_data_*_velociraptor_*' \
  --artifacts-dir '/another/checkout/src/processor/conf/velociraptor/artifacts' \
  --apply
```

`--artifacts-dir` takes precedence over `ARTIFACTS_DIR`. One must be supplied, and the directory must already exist. The default `--index-pattern '*'` enumerates concrete open indices, including hidden non-system indices; closed, system and unrecognized index names are reported as excluded. Aliases do not cause duplicate processing. Narrowing the pattern changes which representative indices are available.

### Checkpoints and resume

Each completed artifact is saved in an atomic checkpoint, including artifacts with no valid dates and groups whose indices are all empty. If execution stops, resume with the same endpoint, pattern and artifact directory:

```bash
python poc/discover_artifact_dates.py --resume
```

Completed artifacts are restored without Elasticsearch requests. The interrupted artifact restarts from its first candidate index and uses a new PIT; document pages are not resumed. The checkpoint retains the original index inventory, so newly created indices are considered only by a new scan. A run without `--resume` starts a new discovery and replaces the checkpoint. Runs made with an older script version without checkpoints cannot be recovered.

The checkpoint path is selected by `--state-file`, then `ARTIFACT_DATES_STATE`, then `.discover-artifact-dates.state.json` in the current working directory. The default filename is ignored by Git. To keep a checkpoint at a fixed location:

```bash
export ARTIFACT_DATES_STATE='/path/to/discovery-state.json'
python poc/discover_artifact_dates.py
python poc/discover_artifact_dates.py --resume --apply
```

`--resume` rejects a missing, corrupt or incompatible checkpoint. Compatibility includes the checkpoint/analysis version, Elasticsearch endpoint, index pattern and artifact directory. Checkpoints store inventory, completed evidence and counters, without credentials, raw document values, PIT IDs or search cursors. Keep a completed checkpoint to apply a preview later: `--resume --apply` rereads the current YAML files and recomputes differences from the saved discovery. It also lets an interrupted application finish without rescanning completed artifacts. Start a new scan when fresh Elasticsearch evidence is required.

### Discovery and date versus time handling

Each index is assumed to contain one artifact source and one case, and the same artifact is assumed to have the same structure across cases. Recognized names have the form `ir_data_<UUID with underscores>_velociraptor_<artifact>`, optionally followed by a numeric generation suffix such as `-000001`. For example, both `ir_data_f2a247c3_e812_4187_a7e3_d5649afdf0a4_velociraptor_mft-000001` and the same name without `-000001` belong to the `mft` group. Underscores inside the complete artifact name are preserved.

For each group, indices are tried alphabetically until the first nonempty one. The script reads `_source.artifact_source` from one document before fetching its mapping and requires it to match the artifact parsed from the name; a mismatch stops discovery. For example, `artifact_source: mft` selects exactly `mft.yaml`. Only this representative index supplies mapping and value evidence; remaining duplicate indices receive no requests. A representative with no dates or unresolved evidence is still complete and does not trigger another index scan. All-empty groups are reported. Invalid artifact names or paths escaping the configured directory are rejected.

The catalog includes physical `date` and `date_nanos` fields inside objects and `nested` structures. A date multifield contributes its parent path in `_source`, not the multifield name. Runtime fields, aliases, scripted date fields and the four root source paths `acquisition_time`, `collection_time`, `@timestamp` and `forensic_all_dates` are excluded. The same exclusions apply to multifields sourced from those paths. This discovery script does not impose the backfill script's doc-values or 100-field restrictions.

Mapping formats distinguish dates, epochs and time-only values. Mixed or unclassified formats require inspecting original `_source` values; doc values are not used to infer dates because Elasticsearch can attach a date to a time-only input. The script scans the representative with PIT and `search_after`, including later pages, until every unresolved candidate has a confirmed date or the index is exhausted. One confirmed date makes a field eligible. A field requiring value verification is established as time-only only after a complete scan with time evidence and no date or unclassifiable evidence. Null, empty and missing values prove neither case. Unsupported format syntax is reported rather than guessed. Mixed date/time observations are reported; filtering individual values remains the processor's responsibility.

Source values can come from nested objects, arrays or literal keys containing dots. Disabled, synthetic or pruned `_source` cannot provide reliable original-value evidence where that evidence is needed; these cases are reported as unresolved. Discovery based on unambiguous mapping formats does not need to scan every document. No date-like string outside a `date` or `date_nanos` mapping is added.

Value recognition supports common numeric calendar patterns (for example `yyyy-MM-dd`, `dd/MM/yyyy`, `yyyyMMdd`), ISO dates, ordinal/week built-in formats, numeric times, fractions, offsets, quoted literals and optional sections. It is deliberately not a complete Java `DateTimeFormatter` implementation: locale names, named zones and advanced custom tokens remain unresolved. Calendar validation covers years 1–9999; valid Elasticsearch representations outside the supported subset can also remain unresolved. Format alternatives are tried in their declared order, so a numeric clock string is not automatically treated as an epoch. The evidence report includes the mapping format and any unsupported alternatives.

Progress reports completed/total artifact groups and their percentage, the current artifact/index, groups recovered from the checkpoint, duplicate indices skipped, empty indices and artifacts without valid dates. The percentage measures artifact groups, not elapsed time or document coverage. A terminal uses an updating progress line; redirected output uses readable log messages. Completion updates are always emitted, including for resumed, empty and skipped groups.

Scanning uses `--batch-size 1000` and periodic document progress at `--progress-every 10` seconds by default, showing documents read and fields still requiring verification. Change these options to adjust request size or reporting; `--progress-every 0` suppresses periodic scan updates. Requests are sequential and an individual slow request may delay a progress message. PIT contexts are closed on completion and on failure. Source verification may need to read every document when a candidate never yields a confirmed date, so there is no sampling limit that could mistake an early time-only page for complete evidence.

### YAML updates and results

An artifact must have at least one valid discovered date field before its YAML is read, validated or updated. Otherwise its result is `skipped_no_valid_dates`: an existing file is left byte-for-byte unchanged and a missing file is not created. This also leaves old excluded entries untouched and does not reject an invalid YAML file that is being skipped. The report distinguishes no date candidates, only times and unresolved evidence. Existing catalog entries alone do not make an artifact eligible.

For eligible artifacts, existing catalog entries retain their order; duplicates are removed and new fields are appended alphabetically. The four excluded paths and fields conclusively established as time-only are removed from existing lists. Unresolved existing entries are preserved and reported, including entries not observed in the selected mappings (`unobserved_existing`); unresolved new fields are not added. Comments, key order and the other YAML sections are preserved with `ruamel.yaml`. Mapping and sequence indentation are detected separately and retained, as are explicit `---` and `...` document markers. Inconsistent indentation styles within one document cannot be guaranteed. Configurations with no discovered artifact source are left alone.

Missing eligible files are created with only `forensic_all_dates`, using two spaces for mappings and sequence dashes indented by two spaces. No processor mappings or other sections are invented. All Elasticsearch discovery and eligible YAML validation complete before any YAML is written. HTTP errors, timeouts, failed shards and invalid eligible YAML abort before the YAML write phase; completed discovery checkpoints remain available for resume. File replacement is atomic per file and verified by rereading it; the whole set is not a multi-file transaction, so a filesystem failure during application can leave earlier files updated. Use the Git diff to inspect or revert changes; the script creates no backups.

The report identifies added, removed and unresolved fields, created/updated/unchanged files, skipped indices and incomplete coverage. Exit codes are `0` for complete success, `2` for a completed report with unresolved evidence or relevant skips, `1` for an error and `130` for interruption. An incomplete report can still describe valid changes; inspect its coverage details as well as the diff before relying on the catalog.

Run the offline tests from the repository root with the dependency installed:

```bash
python -m unittest discover -s poc
```

Tests use simulated Elasticsearch responses and temporary YAML files to cover naming with/without a generation, representative selection across cases, empty-index fallback, date/time formats, multi-page verification, source limitations, checkpoint resume and compatibility, progress counters, file creation, indentation/marker preservation, skipping YAML without valid dates, preview, errors, PIT cleanup and idempotence. They do not establish behavior against a live cluster or the real artifact configuration checkout.
