#!/usr/bin/env python3
"""Populate one ES8 development index with an indexed array of existing dates.

Python 3 standard library only. Preview is the default; --apply writes.
Reads formatted doc values, not raw date strings. Does not modify templates.
"""

import argparse
import base64
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import os
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


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


def discover(properties, prefix="", nested=False):
    """Return physical date fields and why a field cannot be read by this POC."""
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
            found[path] = (spec, reason)
        found.update(discover(spec.get("properties", {}), path + ".", inside_nested))
        found.update(discover(spec.get("fields", {}), path + ".", inside_nested))
    return found


def select_fields(mapping, target, requested):
    if mapping.get("_source", {}).get("enabled") is False:
        raise Error("Updating documents requires _source to be enabled.")
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
            and "epoch_millis" in target_mapping.get(
                "format", "strict_date_optional_time||epoch_millis").split("||")
            and not target_mapping.get("script")
            and not target_mapping.get("copy_to")
            and not target_mapping.get("ignore_malformed", False)
        )
        if not compatible:
            raise Error("Existing target mapping is incompatible; choose another --target.")
    all_fields = discover(mapping.get("properties", {}))
    all_fields.pop(target, None)
    names = sorted(set(requested or all_fields))
    if not names:
        raise Error("No mapped source date fields were found.")
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


def make_updates(hits, fields, target, routing_required):
    lines, previews = [], []
    skipped = 0
    for hit in hits:
        # An unindexed or empty target in _source is still existing user data.
        if target in hit.get("_source", {}):
            skipped += 1
            continue
        values = sorted({epoch_millis(value) for field in fields
                         for value in hit.get("fields", {}).get(field, [])})
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


def run(client, args):
    if not re.fullmatch(r"[a-z0-9_.-]+", args.index) or args.index == "_all":
        raise Error("Use one literal concrete index name, without aliases or wildcards.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.target):
        raise Error("--target must be a simple root field name, e.g. forensic_all_dates.")
    if args.max_docs < 0 or not 1 <= args.batch_size <= 1000:
        raise Error("--max-docs must be >= 0; --batch-size must be between 1 and 1000.")
    path = "/" + quote(args.index, safe="")
    mappings = client.request("GET", path + "/_mapping")
    if list(mappings) != [args.index]:
        raise Error("The supplied name resolves to an alias or multiple indices; use a concrete index.")
    mapping = mappings[args.index]["mappings"]
    requested = [s.strip() for s in args.fields.split(",") if s.strip()] if args.fields else None
    fields, nanos = select_fields(mapping, args.target, requested)
    query = {"bool": {"must_not": [{"exists": {"field": args.target}}]}}
    count = client.request("POST", path + "/_count", {"query": query})
    check_search(count)
    print(json.dumps({"mode": "APPLY" if args.apply else "PREVIEW",
                      "index": args.index, "target": args.target, "source_fields": fields,
                      "documents_without_indexed_target": count["count"],
                      "max_docs": args.max_docs or "all",
                      "coverage": "explicit subset" if requested else "physical mapped dates"}, indent=2))
    if nanos:
        print("NOTE: date_nanos values will be floored to milliseconds; originals are retained.")
    if mapping.get("runtime"):
        print("NOTE: runtime fields are not copied; this POC reads physical date fields only.")
    if args.apply and args.target not in mapping.get("properties", {}):
        client.request("PUT", path + "/_mapping", {"properties": {args.target: {
            "type": "date", "format": "strict_date_optional_time||epoch_millis",
            "index": True, "doc_values": True, "ignore_malformed": False}}})
    stats = {"scanned": 0, "prepared": 0, "updated": 0, "noops": 0, "skipped": 0, "failed": 0}
    pit_id = None
    shown = 0
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
                    "seq_no_primary_term": True, "_source": [args.target],
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
                hits, fields, args.target, mapping.get("_routing", {}).get("required", False))
            stats["scanned"] += len(hits)
            stats["skipped"] += skipped
            stats["prepared"] += len(hits) - skipped
            for preview in previews[:max(0, 3 - shown)]:
                print("Example: " + json.dumps(preview))
                shown += 1
            if args.apply and payload:
                result = client.request("POST", path + "/_bulk?refresh=true", payload, ndjson=True)
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
    finally:
        if pit_id:
            try:
                client.request("DELETE", "/_pit", {"id": pit_id})
            except Error:
                print("NOTE: PIT cleanup failed; its keep_alive will expire.", file=sys.stderr)
        print("Summary: " + json.dumps(stats))
    if not args.apply:
        print("Preview complete: no mappings or documents changed. Add --apply to populate the field.")
    else:
        print("Applied to the selected documents. This does not populate future imports or modify Kibana.")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ES_URL", "http://localhost:9200"))
    parser.add_argument("--index", required=True, help="One concrete development index, not the case alias")
    parser.add_argument("--target", default="forensic_all_dates")
    parser.add_argument("--fields", help="Optional comma-separated source date fields; otherwise discover all")
    parser.add_argument("--max-docs", type=int, default=1000, help="Maximum documents examined; 0 = all")
    parser.add_argument("--batch-size", type=int, default=200)
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
