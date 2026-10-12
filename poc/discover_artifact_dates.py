#!/usr/bin/env python3
"""Discover forensic date source paths in ES8 and merge artifact YAML catalogs.

Preview is the default. --apply writes local YAML files, without backups.
Elasticsearch documents, mappings and settings are never modified.
"""

import argparse
import base64
import copy
from dataclasses import dataclass
import difflib
import io
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tempfile
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


TARGET = "forensic_all_dates"
EXCLUDED = {"acquisition_time", "collection_time", "@timestamp", TARGET}


class Error(Exception):
    pass


class Client:
    """Small HTTP client using the POC's authentication and TLS conventions."""

    def __init__(self, url, ca_cert=None):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise Error("--url must be an Elasticsearch HTTP(S) endpoint.")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise Error("Use ES_* environment variables for credentials, not the URL.")
        self.url = url.rstrip("/")
        self.context = ssl.create_default_context(cafile=ca_cert)
        self.headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("ES_API_KEY")
        username, password = os.environ.get("ES_USERNAME"), os.environ.get("ES_PASSWORD")
        if api_key:
            self.headers["Authorization"] = "ApiKey " + api_key
        elif username is not None and password is not None:
            token = base64.b64encode((username + ":" + password).encode()).decode()
            self.headers["Authorization"] = "Basic " + token
        elif username is not None or password is not None:
            raise Error("Set both ES_USERNAME and ES_PASSWORD, or ES_API_KEY.")

    def request(self, method, path, body=None):
        # Permit only metadata reads, searches and temporary PIT lifecycle calls.
        endpoint = path.split("?", 1)[0]
        allowed = (
            method == "GET" and (endpoint.startswith("/_resolve/index/") or
                                 endpoint.endswith(("/_mapping", "/_settings")))
            or method == "POST" and endpoint.endswith(("/_search", "/_pit"))
            or method == "DELETE" and endpoint == "/_pit"
        )
        if not allowed:
            raise Error("Refusing an Elasticsearch operation outside read-only discovery.")
        data = None if body is None else json.dumps(body).encode()
        try:
            request = Request(self.url + path, data=data, headers=self.headers, method=method)
            with urlopen(request, context=self.context, timeout=90) as response:
                return json.load(response)
        except HTTPError as exc:
            # Do not expose response bodies, documents or credentials.
            raise Error(f"HTTP {exc.code} from {method} {path}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise Error(f"Transport failure ({type(exc).__name__}) during {method} {path}") from None
        except ValueError:
            raise Error(f"Invalid JSON from {method} {path}") from None


def emit(kind, value):
    print(kind + ": " + json.dumps(value, ensure_ascii=False), flush=True)


def check_search(response):
    if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
        raise Error("Search timed out or had shard failures; refusing partial input.")


def source_values(value, path):
    """Read objects, arrays and literal dotted keys without coercing scalars."""
    if isinstance(value, list):
        for item in value:
            yield from source_values(item, path)
    elif not path:
        yield value
    elif isinstance(value, dict):
        parts = path.split(".")
        for end in range(1, len(parts) + 1):
            key = ".".join(parts[:end])
            if key in value:
                yield from source_values(value[key], ".".join(parts[end:]))


def mapped_dates(mapping):
    """Yield (mapped path, original source path, specification)."""
    runtime = mapping.get("runtime", {})

    def walk(properties, prefix="", source_path=None):
        for name, spec in properties.items():
            path = prefix + name
            original = source_path or path
            if spec.get("enabled") is False or "script" in spec or path in runtime:
                continue
            if spec.get("type") in ("date", "date_nanos"):
                yield path, original, spec
            yield from walk(spec.get("properties", {}), path + ".", source_path)
            yield from walk(spec.get("fields", {}), path + ".", original)

    if mapping.get("enabled") is not False:
        yield from walk(mapping.get("properties", {}))


def concrete_indices(client, pattern):
    response = client.request("GET", "/_resolve/index/" + quote(pattern, safe=",*") +
                              "?expand_wildcards=all")
    entries = {entry["name"]: entry for entry in response.get("indices", [])}
    referred = {index for alias in response.get("aliases", []) for index in alias["indices"]}
    referred.update(index for stream in response.get("data_streams", [])
                    for index in stream["backing_indices"])
    # A pattern may match an alias/stream but not the underlying concrete names.
    for name in sorted(referred - entries.keys()):
        resolved = client.request("GET", "/_resolve/index/" + quote(name, safe="") +
                                  "?expand_wildcards=all")
        for entry in resolved.get("indices", []):
            entries[entry["name"]] = entry
        if name not in entries:
            raise Error(f"Cannot resolve concrete index {name}.")
    return [entries[name] for name in sorted(entries)]


def artifact_path(directory, source):
    if (not isinstance(source, str) or not source or source.strip() != source or
            source in (".", "..") or any(c in source for c in "/\\") or
            any(ord(c) < 32 or ord(c) == 127 for c in source)):
        raise Error("artifact_source must be a nonempty filename stem without path separators.")
    path = directory / (source + ".yaml")
    if path.is_symlink() or path.resolve().parent != directory:
        raise Error(f"Refusing an artifact path outside its directory or a symlink: {path.name}")
    if path.exists() and not path.is_file():
        raise Error(f"Artifact configuration is not a regular file: {path.name}")
    return path


def original_source_issue(client, index, mapping):
    source = mapping.get("_source", {})
    if source.get("enabled") is False:
        return "_source disabled"
    if source.get("includes") or source.get("excludes"):
        return "_source pruned by mapping"
    settings = client.request("GET", "/" + quote(index, safe="") +
                              "/_settings?flat_settings=true&include_defaults=true")[index]
    mode = settings.get("settings", {}).get("index.mapping.source.mode",
           settings.get("defaults", {}).get("index.mapping.source.mode", source.get("mode", "stored")))
    if mode == "synthetic" or source.get("mode") == "synthetic":
        return "synthetic _source does not preserve original date/time representation"
    return None


def scan_values(client, index, pending, args, report, progress=None):
    """Resolve ambiguous candidates; only a complete scan can prove time-only."""
    pit_id = None
    last_progress = monotonic()
    try:
        opened = client.request("POST", "/" + quote(index, safe="") +
                                "/_pit?keep_alive=2m&allow_partial_search_results=false")
        pit_id = opened["id"]
        check_search(opened)
        after = None
        complete = False
        while any(item["status"] != "date" for item in pending):
            paths = sorted({item["field"] for item in pending if item["status"] != "date"})
            body = {"size": args.batch_size, "query": {"match_all": {}},
                    "pit": {"id": pit_id, "keep_alive": "2m"}, "sort": ["_shard_doc"],
                    "track_total_hits": False, "_source": paths}
            if after is not None:
                body["search_after"] = after
            response = client.request("POST", "/_search?allow_partial_search_results=false", body)
            pit_id = response.get("pit_id", pit_id)
            check_search(response)
            hits = response["hits"]["hits"]
            if not hits:
                complete = True
                break
            for hit in hits:
                if hit.get("_index") != index:
                    raise Error("Search returned an unexpected concrete index.")
                source = hit.get("_source")
                if not isinstance(source, dict):
                    raise Error(f"Original _source missing during scan of {index}.")
                for item in pending:
                    if item["status"] == "date":
                        continue
                    for value in source_values(source, item["field"]):
                        kind = item["parser"].classify(value)
                        if kind == "date":
                            item["saw_date"] = True
                            item["status"] = "date"
                            item["reason"] = "original value with a date found"
                        elif kind == "time":
                            item["saw_time"] = True
                        elif kind != "empty":
                            item["saw_unknown"] = True
            report["scanned_documents"] += len(hits)
            next_after = hits[-1]["sort"]
            if next_after == after:
                raise Error("Search pagination did not advance.")
            after = next_after
            now = monotonic()
            if args.progress_every and now - last_progress >= args.progress_every:
                remaining = sum(i["status"] != "date" for i in pending)
                if progress is not None:
                    progress.show(index=index, unresolved_fields=remaining)
                else:
                    emit("Progress", {"index": index, "scanned_documents": report["scanned_documents"],
                                      "unresolved_fields": remaining})
                last_progress = now
        for item in pending:
            if item["status"] == "date":
                continue
            if complete and item["saw_time"] and not item["saw_unknown"]:
                item["status"] = "time"
                item["reason"] = "complete scan found only times"
            else:
                item["reason"] = ("unclassifiable original values" if item["saw_unknown"] else
                                  "no nonempty classifiable original values")
    finally:
        if pit_id:
            already_failing = sys.exc_info()[0] is not None
            try:
                closed = client.request("DELETE", "/_pit", {"id": pit_id})
                if closed.get("succeeded") is False:
                    raise Error("PIT cleanup was not successful.")
            except Error as exc:
                # Never hide an earlier search failure, but prevent writes on cleanup failure.
                if already_failing:
                    emit("Warning", {"index": index, "reason": "PIT cleanup failed; it will expire"})
                else:
                    raise exc


INDEX_NAME = re.compile(
    r"^ir_data_[0-9a-fA-F]{8}(?:_[0-9a-fA-F]{4}){3}_[0-9a-fA-F]{12}"
    r"_velociraptor_(?P<artifact>[a-z0-9][a-z0-9_.-]*?)(?:-[0-9]+)?$"
)
STATE_VERSION = 1  # Bump when discovery/classification semantics change.


def group_indices(entries):
    groups, skipped = {}, []
    for entry in entries:
        index, attrs = entry["name"], entry.get("attributes", [])
        match = INDEX_NAME.fullmatch(index)
        reason = ("system" if "system" in attrs else "not open" if "open" not in attrs else
                  "unrecognized index name" if match is None else None)
        if reason:
            skipped.append({"index": index, "status": "skipped", "reason": reason})
        else:
            groups.setdefault(match["artifact"], []).append(index)
    return {source: sorted(names) for source, names in sorted(groups.items())}, skipped


class Checkpoint:
    """Persist only completed artifact evidence, never source values or PIT cursors."""

    def __init__(self, args, directory):
        self.path = Path(args.state_file).expanduser().absolute()
        if self.path.resolve().parent == directory and self.path.suffix.lower() in (".yaml", ".yml"):
            raise Error("Checkpoint path must not overwrite an artifact YAML.")
        parts = urlsplit(args.url)
        if (parts.scheme not in ("http", "https") or not parts.netloc or parts.username or
                parts.password or parts.query or parts.fragment):
            raise Error("Use an HTTP(S) endpoint without credentials or query parameters.")
        self.binding = {"url": args.url.rstrip("/"), "index_pattern": args.index_pattern,
                        "artifacts_dir": str(directory)}
        self.data = None
        self.check_path()
        if args.resume:
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.validate()
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise Error("Missing or corrupt checkpoint: " + str(self.path)) from None

    def check_path(self):
        if (self.path.is_symlink() or not self.path.parent.is_dir() or
                (self.path.exists() and not self.path.is_file())):
            raise Error("Checkpoint must be a regular file in an existing directory: " + str(self.path))

    def validate(self):
        data = self.data
        if not isinstance(data, dict):
            raise ValueError("checkpoint root")
        if data.get("version") != STATE_VERSION:
            raise Error("Incompatible checkpoint version; start a new scan without --resume.")
        if data.get("binding") != self.binding:
            raise Error("Checkpoint endpoint, index pattern or artifact directory differs; start a new scan.")
        entries, results = data["inventory"], data["results"]
        if not isinstance(entries, list) or not isinstance(results, dict):
            raise ValueError("checkpoint collections")
        names = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or
                    not isinstance(entry.get("attributes"), list) or
                    any(not isinstance(attr, str) for attr in entry["attributes"]) or entry["name"] in names):
                raise ValueError("checkpoint inventory")
            names.add(entry["name"])
        groups, _ = group_indices(entries)
        for source, result in results.items():
            if source not in groups or not isinstance(result, dict):
                raise ValueError("checkpoint artifact")
            artifact = result["artifact"]
            if not isinstance(artifact, dict):
                raise ValueError("checkpoint artifact summary")
            for key in ("fields", "time_only", "unresolved", "mixed", "excluded"):
                values = artifact[key]
                if (not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)
                        or len(values) != len(set(values))):
                    raise ValueError("checkpoint field lists")
            representative = artifact["representative_index"]
            if representative is not None and representative not in groups[source]:
                raise ValueError("checkpoint representative")
            expected = "completed" if artifact["fields"] else "skipped_no_valid_dates"
            if artifact["status"] != expected or not isinstance(artifact["reason"], str):
                raise ValueError("checkpoint completion status")
            if not isinstance(artifact["evidence"], list):
                raise ValueError("checkpoint evidence")
            for item in artifact["evidence"]:
                if (not isinstance(item, dict) or item.get("index") != representative or
                        item.get("status") not in ("date", "time", "unknown") or
                        any(not isinstance(item.get(k), str) for k in ("field", "mapped_field", "reason", "format"))):
                    raise ValueError("checkpoint evidence")
            indices = result["indices"]
            if (not isinstance(indices, list) or len(indices) != len(groups[source]) or
                    {item["index"] for item in indices} != set(groups[source]) or
                    any(item.get("status") not in ("scanned", "skipped") for item in indices)):
                raise ValueError("checkpoint index results")
            if type(result["scanned_documents"]) is not int or result["scanned_documents"] < 0:
                raise ValueError("checkpoint document count")

    def start(self, entries):
        self.data = {"version": STATE_VERSION, "binding": self.binding,
                     "inventory": entries, "results": {}}
        self.save()

    def save(self):
        self.check_path()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".artifact-dates-state-",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write((json.dumps(self.data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class Progress:
    def __init__(self, report, total, recovered):
        self.report, self.total, self.recovered = report, total, recovered
        self.artifact = None
        self.current_empty_indices = 0
        self.context = None
        self.tty = sys.stderr.isatty()
        self.active_line = False

    def show(self, source=None, index=None, unresolved_fields=None, finished=False):
        if source is not None:
            self.artifact = source
        report = self.report
        completed = len(report["artifacts"])
        value = {"completed_artifacts": completed, "total_artifacts": self.total,
                 "percent": round(100 * completed / self.total, 1) if self.total else 100.0,
                 "current_artifact": self.artifact, "current_index": index,
                 "recovered_artifacts": self.recovered,
                 "duplicate_indices": sum(i.get("reason") == "duplicate artifact" for i in report["indices"]),
                 "empty_indices": (self.current_empty_indices +
                                   sum(i.get("reason") == "empty index" for i in report["indices"])),
                 "skipped_no_valid_dates": sum(a["status"] == "skipped_no_valid_dates"
                                               for a in report["artifacts"].values()),
                 "scanned_documents": report["scanned_documents"]}
        if unresolved_fields is not None:
            value["unresolved_fields"] = unresolved_fields
        report["progress"] = value
        if self.tty:
            context = (self.artifact, index)
            if index is not None and context != self.context:
                self.close()
                print(f"Artifact: {self.artifact}\nIndex: {index}", file=sys.stderr, flush=True)
                self.context = context
            line = (f"Artifacts {completed}/{self.total} ({value['percent']:g}%) | "
                    f"resumed:{self.recovered} dup:{value['duplicate_indices']} "
                    f"empty:{value['empty_indices']} no-dates:{value['skipped_no_valid_dates']} "
                    f"docs:{value['scanned_documents']}")
            if unresolved_fields is not None:
                line += f" pending:{unresolved_fields}"
            try:
                width = max(1, os.get_terminal_size(sys.stderr.fileno()).columns - 1)
            except (OSError, AttributeError):
                width = 119
            # Long index names are printed once above the progress line, so
            # periodic updates never wrap and leave stale rows on the terminal.
            if len(line) > width:
                line = line[:max(0, width - 1)] + "…"
            print("\r\033[2K" + line, end="\n" if finished else "", file=sys.stderr, flush=True)
            self.active_line = not finished
        else:
            print("Progress: " + json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)

    def close(self):
        if self.active_line:
            print(file=sys.stderr, flush=True)
            self.active_line = False


def summarize_candidates(candidates, excluded, representative):
    by_field = {}
    evidence = [{k: v for k, v in item.items() if k != "parser"} for item in candidates]
    for item in evidence:
        by_field.setdefault(item["field"], []).append(item)
    summary = {"fields": [], "time_only": [], "unresolved": [], "mixed": [],
               "excluded": sorted(excluded), "evidence": evidence,
               "representative_index": representative}
    for field, items in sorted(by_field.items()):
        statuses = {item["status"] for item in items}
        bucket = "fields" if "date" in statuses else "time_only" if statuses == {"time"} else "unresolved"
        summary[bucket].append(field)
        if any(item["saw_date"] for item in items) and any(item["saw_time"] for item in items):
            summary["mixed"].append(field)
    summary["status"] = "completed" if summary["fields"] else "skipped_no_valid_dates"
    summary["reason"] = ("valid dates found" if summary["fields"] else
                         "all indices empty" if representative is None else
                         "unresolved" if summary["unresolved"] else
                         "only_times" if summary["time_only"] else "no_candidates")
    return summary


def discover_artifact(client, args, source, indices, report, progress):
    statuses, candidates, excluded = [], [], set(EXCLUDED)
    representative = None
    before = report["scanned_documents"]
    for position, index in enumerate(indices):
        progress.current_empty_indices = len(statuses)
        progress.show(source=source, index=index)
        path = "/" + quote(index, safe="")
        identity = client.request("POST", path + "/_search?allow_partial_search_results=false",
                                  {"size": 1, "track_total_hits": False, "sort": ["_doc"],
                                   "query": {"match_all": {}}, "_source": ["artifact_source"]})
        check_search(identity)
        hits = identity["hits"]["hits"]
        if not hits:
            statuses.append({"index": index, "status": "skipped", "reason": "empty index"})
            continue
        hit = hits[0]
        if hit.get("_index") != index:
            raise Error("Artifact identity search returned an unexpected index.")
        if not isinstance(hit.get("_source"), dict) or "artifact_source" not in hit["_source"]:
            raise Error("artifact_source unavailable in nonempty index " + index)
        if hit["_source"]["artifact_source"] != source:
            raise Error("artifact_source does not match the artifact in index name: " + index)
        representative = index
        mappings = client.request("GET", path + "/_mapping")
        if set(mappings) != {index}:
            raise Error("Unexpected mapping response for concrete index " + index)
        mapping = mappings[index]["mappings"]
        for mapped, original, spec in mapped_dates(mapping):
            if original in EXCLUDED:
                excluded.add(mapped)
                continue
            parser = DateFormat(spec.get("format"), spec["type"])
            candidates.append({"index": index, "mapped_field": mapped, "field": original, "parser": parser,
                               "format": parser.format_string, "unsupported_formats": parser.unsupported_formats,
                               "status": parser.kind, "reason": "mapping format", "saw_time": parser.kind == "time",
                               "saw_date": parser.kind == "date", "saw_unknown": False})
        pending = [item for item in candidates if item["status"] == "unknown"]
        if pending:
            issue = original_source_issue(client, index, mapping)
            if issue:
                for item in pending:
                    item["reason"] = issue
            else:
                scan_values(client, index, pending, args, report, progress)
        statuses.append({"index": index, "status": "scanned", "artifact_source": source})
        statuses.extend({"index": duplicate, "status": "skipped", "reason": "duplicate artifact",
                         "artifact_source": source, "representative_index": index}
                        for duplicate in indices[position + 1:])
        break
    return {"artifact": summarize_candidates(candidates, excluded, representative), "indices": statuses,
            "scanned_documents": report["scanned_documents"] - before}


def discover_catalog(client, args, directory, report):
    checkpoint = Checkpoint(args, directory)
    entries = checkpoint.data["inventory"] if args.resume else concrete_indices(client, args.index_pattern)
    groups, skipped = group_indices(entries)
    if not args.resume:
        checkpoint.start(entries)
    report["state_file"] = str(checkpoint.path)
    report["indices"].extend(skipped)
    if not groups:
        report["complete"] = False
        emit("Warning", {"reason": "No eligible artifact indices matched"})
    for source, result in checkpoint.data["results"].items():
        report["artifacts"][source] = copy.deepcopy(result["artifact"])
        report["indices"].extend(result["indices"])
        report["scanned_documents"] += result["scanned_documents"]
    progress = Progress(report, len(groups), len(checkpoint.data["results"]))
    try:
        progress.show()
        for source, indices in groups.items():
            if source in checkpoint.data["results"]:
                continue
            result = discover_artifact(client, args, source, indices, report, progress)
            checkpoint.data["results"][source] = result
            checkpoint.save()
            report["artifacts"][source] = result["artifact"]
            report["indices"].extend(result["indices"])
            progress.current_empty_indices = 0
            progress.show(source=source, index=result["artifact"]["representative_index"], finished=True)
        if any(a["unresolved"] or a["representative_index"] is None for a in report["artifacts"].values()):
            report["complete"] = False
    finally:
        progress.close()


def yaml_handler(original_text=None):
    try:
        from ruamel.yaml import YAML
    except ImportError:
        raise Error("Install ruamel.yaml: python3 -m pip install -r requirements-discovery.txt") from None
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    yaml.width = 4096
    yaml._artifact_preamble = ""
    yaml._artifact_epilogue = None
    mapping_indent, sequence_indent, sequence_offset = 2, 4, 2
    if original_text is not None:
        from ruamel.yaml.events import DocumentStartEvent, DocumentEndEvent
        from ruamel.yaml.nodes import MappingNode, SequenceNode

        # Round-trip YAML preserves comments, but indentation and document markers
        # are emitter settings. Infer mapping and sequence spacing independently:
        # a sequence's content column includes both its dash offset and "- ".
        for event in yaml.parse(original_text):
            if isinstance(event, DocumentStartEvent):
                yaml.explicit_start = event.explicit
                if event.explicit:
                    # ruamel does not retain comments preceding an explicit marker.
                    yaml._artifact_preamble = original_text[:event.start_mark.index]
            elif isinstance(event, DocumentEndEvent):
                yaml.explicit_end = event.explicit
                if event.explicit:
                    yaml._artifact_epilogue = original_text[event.end_mark.index:]
        root = yaml.compose(original_text)
        mapping_steps, sequence_styles, visited = [], [], set()

        def inspect(node):
            if id(node) in visited:
                return
            visited.add(id(node))  # YAML aliases can form cycles.
            if isinstance(node, MappingNode):
                for key, value in node.value:
                    if not node.flow_style and value.start_mark.line > key.start_mark.line:
                        offset = value.start_mark.column - key.start_mark.column
                        if isinstance(value, MappingNode) and not value.flow_style and offset > 0:
                            mapping_steps.append(offset)
                        elif isinstance(value, SequenceNode) and not value.flow_style and offset >= 0:
                            # Empty/flow sequences do not provide block-style evidence.
                            if value.value:
                                first = value.value[0]
                                width = first.start_mark.column - key.start_mark.column
                                if first.start_mark.line >= value.start_mark.line and width >= offset + 2:
                                    sequence_styles.append((width, offset))
                    inspect(value)
            elif isinstance(node, SequenceNode):
                for value in node.value:
                    inspect(value)

        if root is not None:
            inspect(root)
        if mapping_steps:
            mapping_indent = max(dict.fromkeys(mapping_steps), key=mapping_steps.count)
        if sequence_styles:
            sequence_indent, sequence_offset = max(dict.fromkeys(sequence_styles), key=sequence_styles.count)
        # Preserve uniform line endings. Mixed endings follow their first occurrence.
        line_break = re.search(r"\r\n|\n|\r", original_text)
        if line_break:
            yaml.line_break = line_break.group()
    yaml.indent(mapping=mapping_indent, sequence=sequence_indent, offset=sequence_offset)
    return yaml


@dataclass
class FileChange:
    path: Path
    original: object
    updated: object
    mode: int
    summary: dict


def plan_files(directory, artifacts):
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.error import YAMLError

    changes = []
    targets = set()
    for source, artifact in artifacts.items():
        if not artifact["fields"]:
            # Do not open or validate a YAML which discovery cannot enrich.
            path = directory / (source + ".yaml")
            reason = ("unresolved" if artifact["unresolved"] else
                      "only_times" if artifact["time_only"] else "no_candidates")
            summary = {"path": str(path), "status": "skipped_no_valid_dates", "reason": reason,
                       "added": [], "removed": [], "fields": [],
                       "unresolved": list(artifact["unresolved"]), "unobserved_existing": []}
            changes.append(FileChange(path, None, None, 0, summary))
            continue
        path = artifact_path(directory, source)
        # Refuse case-fold collisions on portable/case-insensitive checkouts.
        key = path.name.casefold()
        if key in targets:
            raise Error("Artifact names collide as filenames: " + path.name)
        targets.add(key)
        original = path.read_bytes() if path.exists() else None
        try:
            original_text = original.decode("utf-8") if original is not None else None
            yaml = yaml_handler(original_text)
            # Comment tokens otherwise retain CR and the CRLF emitter adds it again.
            load_text = re.sub(r"\r\n|\r", "\n", original_text) if original is not None else None
            data = yaml.load(load_text) if original is not None else CommentedMap()
        except (YAMLError, UnicodeError):
            raise Error("Invalid UTF-8 YAML: " + str(path)) from None
        if not isinstance(data, dict):
            raise Error("Artifact YAML must contain a mapping: " + str(path))
        previous = data.get(TARGET, [])
        if not isinstance(previous, list) or any(not isinstance(field, str) or not field for field in previous):
            raise Error(f"{path}: {TARGET} must be a list of nonempty strings")
        excluded = set(artifact["excluded"]) | set(artifact["time_only"])
        values, seen = CommentedSeq(), set()
        if isinstance(previous, CommentedSeq):
            values.ca.comment = copy.deepcopy(previous.ca.comment)
        for position, field in enumerate(previous):
            if field not in excluded and field not in seen:
                if isinstance(previous, CommentedSeq) and position in previous.ca.items:
                    values.ca.items[len(values)] = copy.deepcopy(previous.ca.items[position])
                values.append(field)
                seen.add(field)
        values.extend(sorted(set(artifact["fields"]) - seen - excluded))
        added = sorted(set(values) - set(previous))
        removed = sorted(set(previous) - set(values))
        changed = original is None or TARGET not in data or list(previous) != list(values)
        if changed:
            before_other = {key: copy.deepcopy(value) for key, value in data.items() if key != TARGET}
            data[TARGET] = values
            output = io.StringIO()
            output.write(yaml._artifact_preamble)
            yaml.dump(data, output)
            rendered = output.getvalue()
            if yaml._artifact_epilogue is not None:
                rendered = rendered.rstrip("\r\n") + yaml._artifact_epilogue
            # Standalone comment tokens can bypass the emitter's line_break setting.
            rendered = re.sub(r"\r\n|\r|\n", lambda _: yaml.line_break or "\n", rendered)
            updated = rendered.encode("utf-8")
            checked = yaml.load(updated.decode("utf-8"))
            if list(checked[TARGET]) != list(values) or {k: v for k, v in checked.items() if k != TARGET} != before_other:
                raise Error("YAML round-trip changed unrelated content: " + str(path))
        else:
            updated = original
        status = "created" if original is None else "updated" if changed else "unchanged"
        observed = (set(artifact["fields"]) | set(artifact["time_only"]) |
                    set(artifact["unresolved"]) | set(artifact["excluded"]))
        unobserved = sorted(set(values) - observed)
        summary = {"path": str(path), "status": status, "added": added, "removed": removed,
                   "fields": list(values),
                   "unresolved": sorted(set(artifact["unresolved"]) | set(unobserved)),
                   "unobserved_existing": unobserved}
        changes.append(FileChange(path, original, updated,
                                  stat.S_IMODE(path.stat().st_mode) if original is not None else 0o644, summary))
    return changes


def check_unchanged(change):
    path = change.path
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise Error("Artifact path changed during discovery: " + str(path))
    current = path.read_bytes() if path.exists() else None
    if current != change.original:
        raise Error("Artifact file changed during discovery: " + str(path))


def apply_files(changes):
    changes = [change for change in changes if change.summary["status"] != "skipped_no_valid_dates"]
    # Validate every file before the first write; each replacement is atomic.
    for change in changes:
        check_unchanged(change)
    written = 0
    try:
        for change in changes:
            if change.summary["status"] == "unchanged":
                continue
            check_unchanged(change)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(dir=change.path.parent, prefix=".artifact-dates-",
                                                 suffix=".tmp", delete=False) as stream:
                    temp_path = Path(stream.name)
                    stream.write(change.updated)
                    stream.flush()
                    os.fchmod(stream.fileno(), change.mode)
                    os.fsync(stream.fileno())
                if change.original is None:
                    # Exclusive creation: never overwrite a file created since planning.
                    os.link(temp_path, change.path)
                else:
                    os.replace(temp_path, change.path)
                written += 1
                if change.path.read_bytes() != change.updated:
                    raise Error("Read-back mismatch: " + str(change.path))
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
    except (OSError, Error) as exc:
        raise Error(f"Local write failed after {written} file(s); completed files remain. {exc}") from None


def run(client, args):
    if not args.artifacts_dir:
        raise Error("Set ARTIFACTS_DIR or --artifacts-dir.")
    directory = Path(args.artifacts_dir).expanduser().resolve()
    if not directory.is_dir():
        raise Error("Artifact directory does not exist: " + str(directory))
    if not 1 <= args.batch_size <= 1000 or args.progress_every < 0:
        raise Error("--batch-size must be 1..1000 and --progress-every must be >= 0.")
    if not args.index_pattern:
        raise Error("--index-pattern cannot be empty.")
    yaml_handler()  # Check the dependency before making any network requests.
    started = monotonic()
    report = {"mode": "APPLY" if args.apply else "PREVIEW", "complete": True,
              "indices": [], "artifacts": {}, "files": [], "scanned_documents": 0}
    discover_catalog(client, args, directory, report)
    changes = plan_files(directory, report["artifacts"])
    for change in changes:
        report["files"].append(change.summary)
        if change.summary["unresolved"]:
            report["complete"] = False
        if change.original != change.updated:
            old = "" if change.original is None else change.original.decode("utf-8")
            diff = difflib.unified_diff(old.splitlines(keepends=True),
                                        change.updated.decode("utf-8").splitlines(keepends=True),
                                        fromfile=str(change.path) if change.original is not None else "/dev/null",
                                        tofile=str(change.path))
            print("".join(diff), end="", flush=True)
    if args.apply:
        apply_files(changes)
    report["elapsed_seconds"] = round(monotonic() - started, 3)
    emit("Summary", report)
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ES_URL", "http://localhost:9200"))
    parser.add_argument("--ca-cert", default=os.environ.get("ES_CA_CERT"))
    parser.add_argument("--artifacts-dir", default=os.environ.get("ARTIFACTS_DIR"))
    parser.add_argument("--index-pattern", default="*")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--state-file", default=os.environ.get(
        "ARTIFACT_DATES_STATE", ".discover-artifact-dates.state.json"),
        help="Checkpoint path (default: ARTIFACT_DATES_STATE or .discover-artifact-dates.state.json)")
    parser.add_argument("--resume", action="store_true", help="Reuse completed artifacts from the checkpoint")
    parser.add_argument("--apply", action="store_true", help="Write local YAML files without backups")
    args = parser.parse_args(argv)
    if not args.artifacts_dir or not Path(args.artifacts_dir).expanduser().is_dir():
        parser.error("Set ARTIFACTS_DIR or --artifacts-dir to an existing directory.")
    return args


def main(argv=None):
    try:
        args = parse_args(argv)
        result = run(Client(args.url, args.ca_cert), args)
        return 0 if result["complete"] else 2
    except (Error, OSError, ValueError, KeyError, TypeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Use --resume with the same options to recover completed artifacts. "
              "Any local file replacements already completed remain applied.", file=sys.stderr)
        return 130


# The date-format recognizers below are kept in this file so it can be copied alone.
# Explicit, conservative recognizers: this is intentionally not a Java date parser.
import calendar as _calendar
import math as _math
import re as _re
from datetime import date as _calendar_date
from decimal import Decimal as _Decimal, InvalidOperation as _InvalidOperation


_DATE_PARTS = frozenset(("y", "u", "M", "L", "d", "D", "Y", "w", "e"))
_TIME_PARTS = frozenset(("H", "m", "s", "S"))
_BUILTIN_DATE_PATTERNS = {
    "basic_date": "yyyyMMdd",
    "basic_date_time": "yyyyMMdd'T'HHmmss.SSSZ",
    "basic_date_time_no_millis": "yyyyMMdd'T'HHmmssZ",
    "basic_ordinal_date": "yyyyDDD",
    "basic_ordinal_date_time": "yyyyDDD'T'HHmmss.SSSZ",
    "basic_ordinal_date_time_no_millis": "yyyyDDD'T'HHmmssZ",
    "date": "yyyy-MM-dd",
    "date_hour": "yyyy-MM-dd'T'HH",
    "date_hour_minute": "yyyy-MM-dd'T'HH:mm",
    "date_hour_minute_second": "yyyy-MM-dd'T'HH:mm:ss",
    "date_hour_minute_second_fraction": "yyyy-MM-dd'T'HH:mm:ss.SSS",
    "date_hour_minute_second_millis": "yyyy-MM-dd'T'HH:mm:ss.SSS",
    "date_time": "yyyy-MM-dd'T'HH:mm:ss.SSSZ",
    "date_time_no_millis": "yyyy-MM-dd'T'HH:mm:ssZ",
    "ordinal_date": "yyyy-DDD",
    "ordinal_date_time": "yyyy-DDD'T'HH:mm:ss.SSSZ",
    "ordinal_date_time_no_millis": "yyyy-DDD'T'HH:mm:ssZ",
    "year": "yyyy",
    "year_month": "yyyy-MM",
    "year_month_day": "yyyy-MM-dd",
    "basic_week_date": "YYYY'W'wwe",
    "basic_week_date_time": "YYYY'W'wwe'T'HHmmss.SSSZ",
    "basic_week_date_time_no_millis": "YYYY'W'wwe'T'HHmmssZ",
    "week_date": "YYYY-'W'ww-e",
    "week_date_time": "YYYY-'W'ww-e'T'HH:mm:ss.SSSZ",
    "week_date_time_no_millis": "YYYY-'W'ww-e'T'HH:mm:ssZ",
    "weekyear": "YYYY",
    "weekyear_week": "YYYY-'W'ww",
    "weekyear_week_day": "YYYY-'W'ww-e",
}
_BUILTIN_TIME_PATTERNS = {
    "basic_time": "HHmmss.SSSZ",
    "basic_time_no_millis": "HHmmssZ",
    "basic_t_time": "'T'HHmmss.SSSZ",
    "basic_t_time_no_millis": "'T'HHmmssZ",
    "hour": "HH",
    "hour_minute": "HH:mm",
    "hour_minute_second": "HH:mm:ss",
    "hour_minute_second_fraction": "HH:mm:ss.SSS",
    "hour_minute_second_millis": "HH:mm:ss.SSS",
    "time": "HH:mm:ss.SSSZ",
    "time_no_millis": "HH:mm:ssZ",
    "t_time": "'T'HH:mm:ss.SSSZ",
    "t_time_no_millis": "'T'HH:mm:ssZ",
}


def _valid_date_components(parts):
    """Validate calendar/time fields without assigning an invented date to time."""
    numeric = {key: int(value) for key, value in parts.items()
               if key in _DATE_PARTS | _TIME_PARTS and key != "S"}
    year = numeric.get("y", numeric.get("u", 2000))
    month = numeric.get("M", numeric.get("L", 1))
    day = numeric.get("d", 1)
    try:
        _calendar_date(year, month, day)
        if "D" in numeric:
            if not 1 <= numeric["D"] <= (366 if _calendar.isleap(year) else 365):
                return False
            if "d" in numeric or "M" in numeric or "L" in numeric:
                if _calendar_date(year, month, day).timetuple().tm_yday != numeric["D"]:
                    return False
        if "Y" in numeric or "w" in numeric or "e" in numeric:
            _calendar_date.fromisocalendar(numeric.get("Y", 2000),
                                          numeric.get("w", 1), numeric.get("e", 1))
    except (ValueError, OverflowError):
        return False
    if not 0 <= numeric.get("H", 0) <= 23:
        return False
    if not 0 <= numeric.get("m", 0) <= 59 or not 0 <= numeric.get("s", 0) <= 59:
        return False
    for token in ("X", "x", "Z"):
        offset = parts.get(token)
        if offset is None or offset == "Z":
            continue
        digits = offset[1:].replace(":", "")
        hours = int(digits[:2])
        minutes = int(digits[2:4] or "0")
        seconds = int(digits[4:6] or "0")
        if hours > 18 or minutes > 59 or seconds > 59:
            return False
        if hours == 18 and (minutes or seconds):
            return False
    return True


class _DatePattern:
    def __init__(self, pattern):
        self.original = pattern
        self.kind = "unknown"
        self.regex = None
        self.fields = []
        self.epoch = None
        self.lenient_builtin = False
        if pattern in ("epoch_millis", "epoch_second"):
            self.epoch = pattern
            self.kind = "date"
            return
        name = pattern.removeprefix("strict_")
        if name in ("date_optional_time", "date_optional_time_nanos"):
            self.lenient_builtin = not pattern.startswith("strict_")
            # Only the supported ISO subset is recognized; out-of-range or uncommon
            # ES/JDK representations stay unknown, never guessed from punctuation.
            expression = (
                r"(?P<y>[0-9]{4})(?:-(?P<M>[0-9]{2})(?:-(?P<d>[0-9]{2})"
                r"(?:T(?P<H>[0-9]{2})(?::(?P<m>[0-9]{2})(?::(?P<s>[0-9]{2})"
                r"(?:[.,](?P<S>[0-9]{1,9}))?)?)?"
                r"(?P<X>Z|[+-][0-9]{2}(?::?[0-9]{2})?)?)?)?)?"
            )
            if self.lenient_builtin:
                expression = expression.replace("(?P<y>[0-9]{4})", "(?P<y>[0-9]{1,4})")
                for part in ("M", "d", "H", "m", "s"):
                    expression = expression.replace("(?P<" + part + ">[0-9]{2})",
                                                    "(?P<" + part + ">[0-9]{1,2})")
            self.regex = _re.compile(expression)
            self.fields = [(key, key) for key in ("y", "M", "d", "H", "m", "s", "S", "X")]
            self.kind = "date"
            return
        builtin = name in _BUILTIN_DATE_PATTERNS or name in _BUILTIN_TIME_PATTERNS
        self.lenient_builtin = builtin and not pattern.startswith("strict_")
        source = _BUILTIN_DATE_PATTERNS.get(name, _BUILTIN_TIME_PATTERNS.get(name, pattern))
        try:
            self._compile(source, builtin=builtin, variable_fraction=name.endswith("_fraction"),
                          variable_width=self.lenient_builtin and not name.startswith("basic_"))
        except ValueError:
            # All unsupported tokens (locale names, eras, quarters, zone IDs,
            # custom week-years, etc.) conservatively require unresolved evidence.
            self.regex = None
            self.fields = []
            self.kind = "unknown"

    def _compile(self, pattern, *, builtin, variable_fraction, variable_width):
        pieces = []
        depth = 0
        mandatory_date = False
        has_date = False
        has_time = False
        i = 0
        while i < len(pattern):
            char = pattern[i]
            if char == "'":
                if i + 1 < len(pattern) and pattern[i + 1] == "'":
                    pieces.append(_re.escape("'"))
                    i += 2
                    continue
                i += 1
                literal = []
                while i < len(pattern):
                    if pattern[i] == "'":
                        if i + 1 < len(pattern) and pattern[i + 1] == "'":
                            literal.append("'")
                            i += 2
                            continue
                        i += 1
                        break
                    literal.append(pattern[i])
                    i += 1
                else:
                    raise ValueError("unterminated quoted literal")
                pieces.append(_re.escape("".join(literal)))
                continue
            if char == "[":
                depth += 1
                pieces.append("(?:")
                i += 1
                continue
            if char == "]":
                if depth == 0:
                    raise ValueError("unbalanced optional section")
                depth -= 1
                pieces.append(")?")
                i += 1
                continue
            if not char.isalpha():
                if char in "{}#":
                    raise ValueError("reserved pattern character")
                pieces.append(_re.escape(char))
                i += 1
                continue
            end = i + 1
            while end < len(pattern) and pattern[end] == char:
                end += 1
            width = end - i
            numeric = None
            if char in "yu" and width in (1, 2, 4):
                numeric = "[0-9]{1,4}" if width == 1 or variable_width else f"[0-9]{{{width}}}"
            elif char in "MLdHms" and width in (1, 2):
                numeric = "[0-9]{1,2}" if width == 1 or variable_width else "[0-9]{2}"
            elif char == "D" and width in (1, 2, 3):
                numeric = "[0-9]{1,3}" if variable_width else f"[0-9]{{{width},3}}"
            elif char == "S" and 1 <= width <= 9:
                numeric = "[0-9]{1,9}" if variable_fraction else f"[0-9]{{{width}}}"
            elif char in "Xx" and 1 <= width <= 5:
                offsets = {1: r"[+-][0-9]{2}(?:[0-9]{2})?", 2: r"[+-][0-9]{4}",
                           3: r"[+-][0-9]{2}:[0-9]{2}",
                           4: r"[+-][0-9]{4}(?:[0-9]{2})?",
                           5: r"[+-][0-9]{2}:[0-9]{2}(?::[0-9]{2})?"}
                numeric = offsets[width]
                if char == "X":
                    numeric = "(?:Z|" + numeric + ")"
            elif char == "Z" and width in (1, 2, 3, 5):
                numeric = r"[+-][0-9]{4}" if width < 5 else r"(?:Z|[+-][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)"
                if builtin:
                    numeric = r"(?:Z|[+-][0-9]{2}:?[0-9]{2})"
            elif builtin and char in "Ywe":
                numeric = {"Y": "[0-9]{4}", "w": "[0-9]{2}", "e": "[1-7]"}[char]
            if numeric is None:
                raise ValueError("unsupported date pattern token")
            group = "v" + str(len(self.fields))
            self.fields.append((group, char + "2" if char in "yu" and width == 2 else char))
            pieces.append("(?P<" + group + ">" + numeric + ")")
            if char in _DATE_PARTS:
                has_date = True
                mandatory_date |= depth == 0
            has_time |= char in _TIME_PARTS
            i = end
        if depth:
            raise ValueError("unbalanced optional section")
        self.regex = _re.compile("".join(pieces))
        self.kind = "date" if mandatory_date else "time" if has_time and not has_date else "unknown"
        if not has_date and not has_time:
            raise ValueError("pattern has no temporal component")

    def classify(self, value):
        """None means known non-match; unknown means unsupported/ambiguous."""
        if self.epoch is not None:
            if not _re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
                return None
            try:
                number = _Decimal(value)
                millis = number * (1000 if self.epoch == "epoch_second" else 1)
                if not _Decimal(-(2 ** 63)) <= millis <= _Decimal(2 ** 63 - 1):
                    return None
            except _InvalidOperation:
                return None
            return "date"
        if self.regex is None:
            return "unknown"
        match = self.regex.fullmatch(value)
        if match is None:
            # A miss in the supported lenient subset is not proof that ES would
            # reject it. Do not fall through and reinterpret a year as a clock,
            # or a clock as an epoch, in a later format alternative.
            return "unknown" if self.lenient_builtin else None
        parts = {}
        for group, token in self.fields:
            captured = match.group(group)
            if captured is None:
                continue
            if token in ("y2", "u2"):
                token = token[0]
                captured = str(2000 + int(captured))
            if token in parts and parts[token] != captured:
                return None
            parts[token] = captured
        if not _valid_date_components(parts):
            return "unknown" if self.lenient_builtin else None
        if parts.keys() & _DATE_PARTS:
            return "date"
        if parts.keys() & _TIME_PARTS:
            return "time"
        return "unknown"


class DateFormat:
    """Classify ES mapping formats and original scalar values conservatively.

    ``kind`` proves a mapping is date-only or time-only. ``unknown`` includes
    mixed alternatives and optional dates, so callers must inspect _source.
    Unsupported alternatives are never skipped to manufacture a later match.
    """

    def __init__(self, format_string=None, field_type="date"):
        if format_string is None:
            prefix = "strict_date_optional_time_nanos" if field_type == "date_nanos" else "strict_date_optional_time"
            format_string = prefix + "||epoch_millis"
        if not isinstance(format_string, str):
            raise ValueError("date format must be a string")
        self.format_string = format_string
        self.patterns = [_DatePattern(part) for part in format_string.split("||")]
        kinds = {pattern.kind for pattern in self.patterns}
        self.kind = next(iter(kinds)) if len(kinds) == 1 else "unknown"
        self.unsupported_formats = [pattern.original for pattern in self.patterns
                                    if pattern.regex is None and pattern.epoch is None]

    def classify(self, value):
        if value is None or isinstance(value, str) and not value.strip():
            return "empty"
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return "unknown"
        if isinstance(value, float) and not _math.isfinite(value):
            return "unknown"
        # Convert JSON numeric scalars without exponent notation; strings are not
        # normalized, trimmed or heuristically reinterpreted as dates.
        text = format(_Decimal(str(value)), "f") if isinstance(value, float) else str(value)
        for pattern in self.patterns:
            result = pattern.classify(text)
            if result is not None:
                return result
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
