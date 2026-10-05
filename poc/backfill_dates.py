#!/usr/bin/env python3
"""Populate one ES8 development index with an indexed array of existing dates.

Python 3 standard library only. Preview is the default; --apply writes.
Copies original ISO date strings from _source, checked against doc values.
Does not modify templates or overwrite an existing target field.
"""

import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import os
import re
import ssl
import sys
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


EXCLUDED_SOURCE_FIELDS = {"acquisition_time"}


class Error(Exception):
    pass


class Client:
    def __init__(self, url, ca_cert=None):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise Error("--url must be an Elasticsearch HTTP(S) endpoint.")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise Error("Use ES_* environment variables for credentials, not the URL.")
        self.url = url.rstrip("/")
        self.context = ssl.create_default_context(cafile=ca_cert)
        self.headers = {}
        api_key = os.environ.get("ES_API_KEY")
        username, password = os.environ.get("ES_USERNAME"), os.environ.get("ES_PASSWORD")
        if api_key:
            self.headers["Authorization"] = "ApiKey " + api_key
        elif username is not None and password is not None:
            token = base64.b64encode((username + ":" + password).encode()).decode()
            self.headers["Authorization"] = "Basic " + token
        elif username is not None or password is not None:
            raise Error("Set both ES_USERNAME and ES_PASSWORD, or ES_API_KEY.")

    def request(self, method, path, body=None, ndjson=False):
        data = None if body is None else (
            body.encode() if ndjson else json.dumps(body).encode()
        )
        headers = dict(self.headers)
        headers["Content-Type"] = "application/x-ndjson" if ndjson else "application/json"
        try:
            with urlopen(Request(self.url + path, data=data, headers=headers,
                                 method=method), context=self.context, timeout=90) as response:
                return json.load(response)
        except HTTPError as exc:
            # Avoid logging documents, credentials or a full Elasticsearch error payload.
            try:
                detail = json.loads(exc.read()).get("error", {})
                kind = detail.get("type", "request_error") if isinstance(detail, dict) else "request_error"
            except (ValueError, AttributeError):
                kind = "request_error"
            raise Error(f"HTTP {exc.code} ({kind}) from {method} {path}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise Error(f"Transport failure ({type(exc).__name__}) during {method} {path}; "
                        "a write may have partially completed. No automatic retry was made.") from None


def discover(properties, prefix="", nested=False, source_field=None):
    """Return date mappings, unsupported reasons and their actual _source paths."""
    found = {}
    for name, spec in properties.items():
        path = prefix + name
        if spec.get("enabled") is False:
            continue
        inside_nested = nested or spec.get("type") == "nested"
        if spec.get("type") in ("date", "date_nanos"):
            reason = "nested date" if inside_nested else (
                "doc_values disabled" if spec.get("doc_values") is False else None
            )
            found[path] = (spec, reason, source_field or path)
        found.update(discover(spec.get("properties", {}), path + ".", inside_nested, source_field))
        # Multifields index their parent's value; they have no separate _source key.
        found.update(discover(spec.get("fields", {}), path + ".", inside_nested, source_field or path))
    return found


def select_fields(mapping, target, requested):
    if mapping.get("_source", {}).get("enabled") is False:
        raise Error("Updating documents requires _source to be enabled.")
    if mapping.get("_source", {}).get("mode") == "synthetic":
        raise Error("Synthetic _source cannot guarantee original date strings.")
    if mapping.get("_source", {}).get("includes") or mapping.get("_source", {}).get("excludes"):
        raise Error("This POC does not update indices with pruned _source mappings.")
    if mapping.get("enabled") is False:
        raise Error("The index mapping has enabled:false.")
    if target in mapping.get("runtime", {}):
        raise Error("The target name is already a runtime field; choose another --target.")
    target_mapping = mapping.get("properties", {}).get(target)
    if target_mapping is not None:
        compatible = (
            target_mapping.get("type") == "date"
            and target_mapping.get("index", True)
            and target_mapping.get("doc_values", True)
            and target_mapping.get("format", "strict_date_optional_time||epoch_millis").split("||")[0]
                == "strict_date_optional_time"
            and not target_mapping.get("script")
            and not target_mapping.get("copy_to")
            and not target_mapping.get("ignore_malformed", False)
        )
        if not compatible:
            raise Error("Existing target mapping is incompatible; choose another --target.")
    all_fields = discover(mapping.get("properties", {}))
    all_fields.pop(target, None)
    # Exclude the source field and any multifields indexing that same value.
    excluded = EXCLUDED_SOURCE_FIELDS | {
        name for name, (_, _, source_path) in all_fields.items()
        if source_path in EXCLUDED_SOURCE_FIELDS
    }
    if requested and excluded.intersection(requested):
        raise Error("These source fields are excluded from forensic dates: " +
                    ", ".join(sorted(excluded.intersection(requested))))
    names = sorted(set(requested) if requested else set(all_fields) - excluded)
    if not names:
        raise Error("No eligible mapped source date fields were found (acquisition_time is excluded).")
    unsupported = []
    for name in names:
        if name in mapping.get("runtime", {}):
            unsupported.append(name + " (shadowed by a runtime field)")
        elif name not in all_fields:
            unsupported.append(name + " (not a physical source date field)")
        elif all_fields[name][1]:
            unsupported.append(name + " (" + all_fields[name][1] + ")")
    if unsupported:
        raise Error("Unsupported fields: " + ", ".join(unsupported) +
                    ". Use --fields only for an explicitly partial POC.")
    if len(names) > 100:
        raise Error("More than 100 date fields; select a smaller POC with --fields. "
                    "This script does not change index.max_docvalue_fields_search.")
    return names, any(all_fields[name][0]["type"] == "date_nanos" for name in names)


def epoch_millis(value):
    """Floor sub-millisecond values exactly, without floating point conversion."""
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        result = int(number.to_integral_value(rounding=ROUND_FLOOR))
        if not -(2**63) <= result < 2**63:
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError, OverflowError):
        raise Error("Elasticsearch returned an invalid epoch_millis value.") from None


def check_search(response):
    if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
        raise Error("Search timed out or had shard failures; refusing partial input.")


def source_values(value, path):
    """Read object paths, literal dotted keys and arrays without altering scalars."""
    if isinstance(value, list):
        for item in value:
            yield from source_values(item, path)
    elif not path:
        if value is not None:
            yield value
    elif isinstance(value, dict):
        # Both {"mft": {"fn_atime": ...}} and {"mft.fn_atime": ...} are valid.
        parts = path.split(".")
        for end in range(1, len(parts) + 1):
            key = ".".join(parts[:end])
            if key in value:
                yield from source_values(value[key], ".".join(parts[end:]))


ISO_DATE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?"
    r"(?:Z|[+-](?:0[0-9]|1[0-7]):[0-5][0-9]|[+-]18:00)?)?"
)


def source_epoch_millis(value, field):
    """Parse only for comparison; callers retain the exact original string."""
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        raise Error(f"Field {field} has a non-ISO _source date; exact copying requires ISO strings. "
                    "Use --fields only for an explicitly supported subset.")
    # datetime uses microseconds; discard finer digits for this comparison only.
    comparable = re.sub(r"\.([0-9]+)", lambda m: "." + m[1][:6].ljust(6, "0"), value)
    try:
        parsed = datetime.fromisoformat(comparable.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        raise Error(f"Field {field} has an unsupported ISO _source date.") from None
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def make_updates(hits, fields, target, routing_required, source_paths=None):
    lines, previews = [], []
    skipped = 0
    for hit in hits:
        source = hit.get("_source")
        if not isinstance(source, dict):
            raise Error("Missing _source; original date strings cannot be copied.")
        # An unindexed or empty target in _source is still existing user data.
        if target in source:
            skipped += 1
            continue
        values = []
        seen = set()
        for field in fields:
            indexed = {epoch_millis(value) for value in hit.get("fields", {}).get(field, [])}
            if not indexed:
                continue
            originals = list(source_values(source, (source_paths or {}).get(field, field)))
            represented = {source_epoch_millis(value, field) for value in originals}
            if represented != indexed:
                raise Error(f"Field {field}: _source dates do not match indexed dates. "
                            "Cannot preserve original values with complete coverage; no updates sent for this page.")
            for value in originals:
                if value not in seen:
                    seen.add(value)
                    values.append(value)
        if not values:
            skipped += 1
            continue
        if "_seq_no" not in hit or "_primary_term" not in hit:
            raise Error("Missing optimistic concurrency metadata in search response.")
        action = {"_index": hit["_index"], "_id": hit["_id"],
                  "if_seq_no": hit["_seq_no"], "if_primary_term": hit["_primary_term"]}
        routing = hit.get("_routing")
        if routing is None:
            routing_values = hit.get("fields", {}).get("_routing", [])
            routing = routing_values[0] if routing_values else None
        if routing is not None:
            action["routing"] = routing
        elif routing_required:
            raise Error("Required routing was not returned; no updates sent for this page.")
        lines.extend([json.dumps({"update": action}),
                      json.dumps({"doc": {target: values}})])
        if len(previews) < 3:
            previews.append({"_id": hit["_id"], target: values})
    return "\n".join(lines) + ("\n" if lines else ""), previews, skipped


def progress(stats, started):
    elapsed = max(monotonic() - started, 0.001)
    return {**stats, "elapsed_seconds": round(elapsed, 3),
            "scanned_per_second": round(stats["scanned"] / elapsed, 1),
            "updated_per_second": round(stats["updated"] / elapsed, 1)}


def run(client, args):
    if not re.fullmatch(r"[a-z0-9_.-]+", args.index) or args.index == "_all":
        raise Error("Use one literal concrete index name, without aliases or wildcards.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.target):
        raise Error("--target must be a simple root field name, e.g. forensic_all_dates.")
    if args.max_docs < 0 or not 1 <= args.batch_size <= 1000:
        raise Error("--max-docs must be >= 0; --batch-size must be between 1 and 1000.")
    if args.refresh not in ("final", "batch", "none") or args.progress_every < 0:
        raise Error("--refresh must be final, batch or none; --progress-every must be >= 0.")
    started = last_progress = monotonic()
    path = "/" + quote(args.index, safe="")
    mappings = client.request("GET", path + "/_mapping")
    if list(mappings) != [args.index]:
        raise Error("The supplied name resolves to an alias or multiple indices; use a concrete index.")
    mapping = mappings[args.index]["mappings"]
    requested = [s.strip() for s in args.fields.split(",") if s.strip()] if args.fields else None
    fields, nanos = select_fields(mapping, args.target, requested)
    settings = client.request("GET", path + "/_settings?flat_settings=true&include_defaults=true")[args.index]
    source_mode = settings.get("settings", {}).get("index.mapping.source.mode",
                   settings.get("defaults", {}).get("index.mapping.source.mode", "stored"))
    if source_mode == "synthetic":
        raise Error("Synthetic _source cannot guarantee original date strings.")
    catalog = discover(mapping.get("properties", {}))
    source_paths = {field: catalog[field][2] for field in fields}
    query = {"bool": {"must_not": [{"exists": {"field": args.target}}]}}
    count = client.request("POST", path + "/_count", {"query": query})
    check_search(count)
    print(json.dumps({"mode": "APPLY" if args.apply else "PREVIEW",
                      "index": args.index, "target": args.target, "source_fields": fields,
                      "excluded_source_fields": sorted(EXCLUDED_SOURCE_FIELDS),
                      "documents_without_indexed_target": count["count"],
                      "max_docs": args.max_docs or "all",
                      "batch_size": args.batch_size, "refresh": args.refresh,
                      "representation": "original ISO strings from _source",
                      "coverage": "explicit subset" if requested else
                                  "physical mapped dates excluding acquisition_time"}, indent=2), flush=True)
    if nanos:
        print("NOTE: original date_nanos strings are preserved; the target date field indexes milliseconds.")
    if mapping.get("runtime"):
        print("NOTE: runtime fields are not copied; this POC reads physical date fields only.")
    if args.apply and args.target not in mapping.get("properties", {}):
        client.request("PUT", path + "/_mapping", {"properties": {args.target: {
            "type": "date", "format": "strict_date_optional_time||epoch_millis",
            "index": True, "doc_values": True, "ignore_malformed": False}}})
    stats = {"scanned": 0, "prepared": 0, "updated": 0, "noops": 0, "skipped": 0, "failed": 0}
    pit_id = None
    shown = 0
    bulk_attempted = completed = False
    try:
        opened = client.request("POST", path + "/_pit?keep_alive=2m")
        pit_id = opened["id"]
        check_search(opened)
        after = None
        while args.max_docs == 0 or stats["scanned"] < args.max_docs:
            size = args.batch_size if args.max_docs == 0 else min(
                args.batch_size, args.max_docs - stats["scanned"])
            body = {"size": size, "query": query, "pit": {"id": pit_id, "keep_alive": "2m"},
                    "sort": ["_shard_doc"], "track_total_hits": False,
                    "seq_no_primary_term": True, "_source": sorted({args.target, *source_paths.values()}),
                    "stored_fields": ["_routing"],
                    "docvalue_fields": [{"field": field, "format": "epoch_millis"} for field in fields]}
            if after is not None:
                body["search_after"] = after
            response = client.request("POST", "/_search?allow_partial_search_results=false", body)
            pit_id = response.get("pit_id", pit_id)
            check_search(response)
            hits = response["hits"]["hits"]
            if not hits:
                break
            if any(hit["_index"] != args.index for hit in hits):
                raise Error("Search returned an unexpected index; refusing to update it.")
            payload, previews, skipped = make_updates(
                hits, fields, args.target, mapping.get("_routing", {}).get("required", False), source_paths)
            stats["scanned"] += len(hits)
            stats["skipped"] += skipped
            stats["prepared"] += len(hits) - skipped
            for preview in previews[:max(0, 3 - shown)]:
                print("Example: " + json.dumps(preview))
                shown += 1
            if args.apply and payload:
                bulk_attempted = True
                refresh = "true" if args.refresh == "batch" else "false"
                result = client.request("POST", path + "/_bulk?refresh=" + refresh, payload, ndjson=True)
                items = result.get("items", [])
                if len(items) != len(hits) - skipped:
                    raise Error("Unexpected bulk response length; some writes may have completed.")
                for item in items:
                    update = item["update"]
                    if update.get("error") or update["status"] >= 300:
                        stats["failed"] += 1
                    elif update.get("result") == "noop":
                        stats["noops"] += 1
                    else:
                        stats["updated"] += 1
                if result.get("errors") or stats["failed"]:
                    raise Error("Bulk had failed items (possibly version conflicts). "
                                "Successful updates remain; rerun to read fresh values.")
            after = hits[-1]["sort"]
            now = monotonic()
            if args.progress_every and now - last_progress >= args.progress_every:
                print("Progress: " + json.dumps(progress(stats, started)), flush=True)
                last_progress = now
        completed = True
    finally:
        if pit_id:
            try:
                client.request("DELETE", "/_pit", {"id": pit_id})
            except Error:
                print("NOTE: PIT cleanup failed; its keep_alive will expire.", file=sys.stderr)
        try:
            if bulk_attempted and args.refresh == "final":
                # Also expose successful writes after a partially failed bulk.
                # Refresh after closing the PIT so its old segments can be released.
                try:
                    refreshed = client.request("POST", path + "/_refresh")
                    if refreshed.get("_shards", {}).get("failed", 0):
                        raise Error("Final refresh had shard failures; search visibility is not confirmed.")
                except (Error, ValueError) as exc:
                    if completed:
                        raise Error("Final refresh failed; successful writes remain but search "
                                    "visibility is not confirmed. " + str(exc)) from None
                    # Preserve the original search/bulk/transport error.
                    print("NOTE: Final refresh failed; successful writes remain but may not "
                          "yet be searchable. " + str(exc), file=sys.stderr)
        finally:
            stats.update(progress(stats, started))
            print("Summary: " + json.dumps(stats), flush=True)
    if not args.apply:
        print("Preview complete: no mappings or documents changed. Add --apply to populate the field.")
    else:
        print("Applied to the selected documents. This does not populate future imports or modify Kibana.")
        if args.refresh == "none":
            print("No refresh was requested; search visibility follows the index refresh policy.")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ES_URL", "http://localhost:9200"))
    parser.add_argument("--index", required=True, help="One concrete development index, not the case alias")
    parser.add_argument("--target", default="forensic_all_dates")
    parser.add_argument("--fields", help="Optional comma-separated source date fields; acquisition_time is always excluded")
    parser.add_argument("--max-docs", type=int, default=1000, help="Maximum documents examined; 0 = all")
    parser.add_argument("--batch-size", type=int, default=1000, help="Documents per page/bulk (1-1000; default: 1000)")
    parser.add_argument("--refresh", choices=("final", "batch", "none"), default="final",
                        help="Refresh once on exit (default), after each bulk, or never explicitly")
    parser.add_argument("--progress-every", type=int, default=10,
                        help="Report progress after a batch every N seconds; 0 disables (default: 10)")
    parser.add_argument("--ca-cert", default=os.environ.get("ES_CA_CERT"))
    parser.add_argument("--apply", action="store_true", help="Add mapping and update documents; default is preview")
    args = parser.parse_args()
    try:
        run(Client(args.url, args.ca_cert), args)
    except (Error, OSError, ValueError, KeyError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Successful updates, if any, remain applied.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
