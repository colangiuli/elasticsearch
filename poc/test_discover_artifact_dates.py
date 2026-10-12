"""Offline behavior checks for date discovery and local artifact configuration."""

import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from ruamel.yaml import YAML

import discover_artifact_dates as discovery


MIXED_FORMAT = "yyyy-MM-dd||HH:mm:ss"


def name(source="mft", case=1, generation="000001"):
    stem = f"ir_data_{case:08x}_1234_4567_89ab_123456789abc_velociraptor_{source}"
    return stem if generation is None else f"{stem}-{generation}"


def date(format=None, **extra):
    return {"type": "date", **({"format": format} if format else {}), **extra}


def index(source="mft", properties=None, pages=None, **extra):
    return {"source": source, "mapping": {"properties": properties or {}},
            "pages": pages or [], "attributes": ["open"], **extra}


class FakeClient:
    """No writes are implemented: an accidental ES mutation fails the test."""

    def __init__(self, indices):
        self.indices = copy.deepcopy(indices)
        self.calls = []
        self.pits = {}
        self.counter = 0
        self.page_numbers = {}

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        route = unquote(urlsplit(path).path)
        if method == "GET" and route.startswith("/_resolve/index/"):
            return {"indices": [{"name": name, "attributes": value["attributes"],
                                 "aliases": value.get("aliases", [])}
                                for name, value in self.indices.items()],
                    "aliases": [], "data_streams": []}
        if method == "GET" and route.endswith("/_mapping"):
            name = route[1:-len("/_mapping")]
            return {name: {"mappings": copy.deepcopy(self.indices[name]["mapping"])}}
        if method == "GET" and route.endswith("/_settings"):
            name = route[1:-len("/_settings")]
            return {name: {"settings": {"index.mapping.source.mode":
                                       self.indices[name].get("source_mode", "stored")}}}
        if method == "POST" and route.endswith("/_search") and route != "/_search":
            name = route[1:-len("/_search")]
            spec = self.indices[name]
            if "identity_error" in spec:
                raise discovery.Error(spec["identity_error"])
            source = spec["source"]
            hits = [] if spec.get("empty") else [{"_index": name, "_source": (
                {} if source is None else {"artifact_source": source})}]
            return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": hits}}
        if method == "POST" and route.endswith("/_pit"):
            name = route[1:-len("/_pit")]
            self.counter += 1
            identifier = f"pit-{self.counter}"
            self.pits[identifier] = name
            self.page_numbers[name] = 0
            response = {"id": identifier, "_shards": {"failed": 0}}
            response.update(self.indices[name].get("pit_extra", {}))
            return response
        if method == "POST" and route == "/_search":
            name = self.pits[body["pit"]["id"]]
            spec = self.indices[name]
            number = self.page_numbers[name]
            self.page_numbers[name] += 1
            if spec.get("scan_error") and number >= spec.get("scan_error_after", 0):
                raise discovery.Error(spec["scan_error"])
            if "interrupt_after" in spec and number >= spec["interrupt_after"]:
                raise KeyboardInterrupt()
            pages = spec["pages"]
            sources = pages[number] if number < len(pages) else []
            self.counter += 1
            identifier = f"pit-{self.counter}"
            self.pits[identifier] = name
            hits = [{"_index": name, "_id": f"{number}-{n}", "sort": [number * 1000 + n], "_source": source}
                    for n, source in enumerate(sources)]
            response = {"pit_id": identifier, "timed_out": False,
                        "_shards": {"failed": 0}, "hits": {"hits": hits}}
            response.update(spec.get("search_extra", {}))
            return response
        if method == "DELETE" and route == "/_pit":
            name = self.pits.pop(body["id"])
            if self.indices[name].get("cleanup_error"):
                raise discovery.Error(self.indices[name]["cleanup_error"])
            return {"succeeded": True, "num_freed": 1}
        raise AssertionError(f"Unexpected Elasticsearch operation: {method} {path}")

    def scan_calls(self):
        return [body for method, path, body in self.calls
                if method == "POST" and urlsplit(path).path == "/_search"]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "artifacts"
        self.directory.mkdir()
        self.state_file = Path(self.temporary.name) / "discovery-state.json"

    def options(self, apply=False, **overrides):
        return argparse.Namespace(**{
            "artifacts_dir": str(self.directory), "index_pattern": "*", "apply": apply,
            "batch_size": 1000, "progress_every": 10,
            "url": "http://example.invalid:9200", "state_file": str(self.state_file),
            "resume": False, **overrides})

    def run_discovery(self, client, apply=False, **overrides):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return discovery.run(client, self.options(apply=apply, **overrides))

    def read_yaml(self, source="mft"):
        return YAML(typ="safe").load((self.directory / f"{source}.yaml").read_text())

    def write_yaml(self, text, source="mft"):
        path = self.directory / f"{source}.yaml"
        path.write_text(text)
        return path

    def test_one_representative_per_artifact_nested_multifields_and_exclusions(self):
        properties = {
            "mft": {"properties": {"created": date()}},
            "entries": {"type": "nested", "properties": {"when": {"type": "date_nanos"}}},
            "raw_date": {"type": "keyword", "fields": {"as_date": date()}},
            "alias_date": {"type": "alias", "path": "mft.created"},
            "computed": date(script={"source": "emit(0)"}),
        }
        for field in ["@timestamp", "acquisition_time", "collection_time", "forensic_all_dates"]:
            properties[field] = date(fields={"as_date": date()})
        first = index(properties=properties, aliases=["case-alias"])
        first["mapping"]["runtime"] = {"runtime_date": date()}
        client = FakeClient({name(case=1): first,
                             name(case=2): index(properties={"mft": {"properties": {"modified": date()}}}),
                             name("prefetch"): index("prefetch", {"run_time": date()})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(set(result["artifacts"]["mft"]["fields"]),
                         {"mft.created", "entries.when", "raw_date"})
        self.assertEqual(self.read_yaml()["forensic_all_dates"],
                         ["entries.when", "mft.created", "raw_date"])
        self.assertEqual(self.read_yaml("prefetch")["forensic_all_dates"], ["run_time"])
        self.assertEqual(client.scan_calls(), [])
        self.assertFalse(any(name(case=2) in path for _, path, _ in client.calls))
        mappings = [path for method, path, _ in client.calls if path.endswith("/_mapping")]
        self.assertEqual(len(mappings), 2)
        self.assertEqual(result["artifacts"]["mft"]["representative_index"], name(case=1))

    def test_preview_proposes_missing_file_without_writing(self):
        client = FakeClient({name(): index(properties={"created": date()})})
        result = self.run_discovery(client)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(result["files"][0]["status"], "created")
        self.assertEqual(result["files"][0]["added"], ["created"])

    def test_existing_unobserved_field_is_preserved_and_reported_unresolved(self):
        self.write_yaml("forensic_all_dates: [legacy]\n")
        client = FakeClient({name(): index(properties={"when": date()})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["legacy", "when"])
        self.assertEqual(result["files"][0]["unresolved"], ["legacy"])
        self.assertEqual(result["files"][0]["unobserved_existing"], ["legacy"])
        self.assertFalse(result["complete"])

    def test_legacy_excluded_multifield_entries_are_removed(self):
        self.write_yaml("forensic_all_dates: [acquisition_time.as_date, collection_time.as_date, when]\n")
        properties = {"when": date(), "acquisition_time": date(fields={"as_date": date()}),
                      "collection_time": {"type": "keyword", "fields": {"as_date": date()}}}
        result = self.run_discovery(FakeClient({name(): index(properties=properties)}), apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])
        self.assertEqual(set(result["files"][0]["removed"]),
                         {"acquisition_time.as_date", "collection_time.as_date"})

    def test_apply_preserves_comments_order_and_other_sections_without_backups(self):
        self.write_yaml("# Artifact settings\nforensic_all_dates:\n"
                        "  - z_date # Keep original order\n  - acquisition_time\n"
                        "  - collection_time\n  - '@timestamp'\n  - forensic_all_dates\n"
                        "  - z_date\nmappings:\n  - src_field: EntryNumber # Keep this comment\n"
                        "    dest_field: mft.id\n    action: move\n")
        definitions = {name(): index(properties={"a_date": date(), "z_date": date()})}
        result = self.run_discovery(FakeClient(definitions), apply=True)
        actual = self.read_yaml()
        self.assertEqual(actual["forensic_all_dates"], ["z_date", "a_date"])
        self.assertEqual(actual["mappings"], [{"src_field": "EntryNumber", "dest_field": "mft.id", "action": "move"}])
        text = (self.directory / "mft.yaml").read_text()
        for comment in ["# Artifact settings", "# Keep original order", "# Keep this comment"]:
            self.assertIn(comment, text)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"mft.yaml"})
        self.assertEqual(result["files"][0]["status"], "updated")
        self.assertEqual(set(result["files"][0]["removed"]),
                         {"acquisition_time", "collection_time", "@timestamp", "forensic_all_dates"})
        before = (self.directory / "mft.yaml").read_bytes()
        second = self.run_discovery(FakeClient(definitions), apply=True)
        self.assertEqual(second["files"][0]["status"], "unchanged")
        self.assertEqual((self.directory / "mft.yaml").read_bytes(), before)

    def test_missing_yaml_is_not_created_when_only_clock_formats_exist(self):
        client = FakeClient({name(): index(properties={
            "clock": date("HH:mm:ss"), "clock_tz": date("strict_time"),
            "quoted_clock": date("'year='HH:mm:ss")})})
        result = self.run_discovery(client, apply=True)
        self.assertFalse((self.directory / "mft.yaml").exists())
        self.assertEqual(result["artifacts"]["mft"]["status"], "skipped_no_valid_dates")
        self.assertEqual(result["files"][0]["status"], "skipped_no_valid_dates")
        self.assertEqual(set(result["artifacts"]["mft"]["time_only"]),
                         {"clock", "clock_tz", "quoted_clock"})

    def test_date_in_later_page_confirms_mixed_field_and_closes_latest_pit(self):
        client = FakeClient({name(): index(properties={"when": date(MIXED_FORMAT)},
            pages=[[{"when": "12:13:14"}], [{"when": "2026-10-12"}]])})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])
        self.assertIn("when", result["artifacts"]["mft"]["mixed"])
        self.assertEqual(result["scanned_documents"], 2)
        searches = client.scan_calls()
        self.assertEqual(len(searches), 2)
        self.assertEqual(searches[1]["search_after"], [0])
        self.assertEqual(searches[1]["pit"]["id"], "pit-2")
        self.assertNotIn("docvalue_fields", searches[0])
        self.assertEqual(set(searches[0]["_source"]), {"when"})
        self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-3"}))

    def test_complete_clock_scan_removes_only_proven_clock_fields(self):
        self.write_yaml("forensic_all_dates: [clock, uncertain, unseen, legacy]\n")
        client = FakeClient({name(): index(properties={
            **{field: date(MIXED_FORMAT) for field in ["clock", "uncertain", "unseen"]},
            "valid": date()},
            pages=[[{"clock": "01:02:03", "uncertain": "01:02:03", "unseen": None}],
                   [{"clock": None, "uncertain": "not a date"}]])})
        result = self.run_discovery(client, apply=True)
        artifact = result["artifacts"]["mft"]
        self.assertEqual(artifact["time_only"], ["clock"])
        self.assertTrue({"uncertain", "unseen"}.issubset(artifact["unresolved"]))
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["uncertain", "unseen", "legacy", "valid"])
        self.assertEqual(len(client.scan_calls()), 3)
        self.assertFalse(result["complete"])

    def test_nonempty_representative_without_dates_does_not_try_another_case(self):
        client = FakeClient({
            name(case=1): index(properties={"when": date("HH:mm:ss")}),
            name(case=2): index(properties={"when": date("yyyy-MM-dd")})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(result["artifacts"]["mft"]["fields"], [])
        self.assertEqual(result["artifacts"]["mft"]["time_only"], ["when"])
        self.assertFalse((self.directory / "mft.yaml").exists())
        self.assertFalse(any(name(case=2) in path for _, path, _ in client.calls))

    def test_unresolved_representative_does_not_try_another_case(self):
        original = b"---\nforensic_all_dates: [when, acquisition_time]\n"
        (self.directory / "mft.yaml").write_bytes(original)
        client = FakeClient({
            name(case=1): index(properties={"when": date(MIXED_FORMAT)}, pages=[[{"when": None}]]),
            name(case=2): index(properties={"when": date()})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual((self.directory / "mft.yaml").read_bytes(), original)
        self.assertIn("when", result["artifacts"]["mft"]["unresolved"])
        self.assertFalse(any(name(case=2) in path for _, path, _ in client.calls))

    def test_ambiguous_values_resolve_nested_arrays_dotted_keys_and_multifields(self):
        client = FakeClient({name(): index(properties={
            "rows": {"type": "nested", "properties": {"meta": {"properties": {"when": date(MIXED_FORMAT)}}}},
            "raw": {"type": "keyword", "fields": {"as_date": date(MIXED_FORMAT)}},
            "obj": {"properties": {"when": date(MIXED_FORMAT)}}},
            pages=[[{"rows": [{"meta.when": [None, "2026-10-12"]}],
                     "raw": "2026-10-12", "obj.when": "2026-10-12"}]])})
        self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["obj.when", "raw", "rows.meta.when"])
        self.assertEqual(set(client.scan_calls()[0]["_source"]), {"obj.when", "raw", "rows.meta.when"})

    def test_custom_optional_date_parts_require_actual_calendar_evidence(self):
        client = FakeClient({name(): index(properties={
            "optional": date("[yyyy-MM-dd ]HH:mm:ss"),
            "literal": date("'yyyy-MM-dd 'HH:mm:ss")},
            pages=[[{"optional": "10:30:00"}], [{"optional": "2026-10-12 10:30:00"}]])})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["optional"])
        self.assertIn("literal", result["artifacts"]["mft"]["time_only"])

    def test_epoch_mixed_with_clock_is_recognized_only_when_format_allows_it(self):
        client = FakeClient({name(): index(properties={
            "epoch": date("epoch_millis||HH:mm:ss"), "not_epoch": date(MIXED_FORMAT)},
            pages=[[{"epoch": 1720000000000, "not_epoch": 1720000000000}]])})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["epoch"])
        self.assertIn("not_epoch", result["artifacts"]["mft"]["unresolved"])

    def test_synthetic_or_disabled_source_cannot_confirm_ambiguous_dates(self):
        for source_setting in ["synthetic", "disabled"]:
            with self.subTest(source_setting=source_setting):
                spec = index(properties={"when": date(MIXED_FORMAT)}, pages=[[{"when": "2026-10-12"}]])
                if source_setting == "synthetic":
                    spec["source_mode"] = "synthetic"
                else:
                    spec["mapping"]["_source"] = {"enabled": False}
                result = self.run_discovery(FakeClient({name(): spec}))
                self.assertEqual(result["artifacts"]["mft"]["fields"], [])
                self.assertIn("when", result["artifacts"]["mft"]["unresolved"])
                self.assertFalse(result["complete"])

    def test_hidden_indices_are_scanned_but_system_and_closed_indices_are_skipped(self):
        definitions = {
            name(): index(properties={"when": date()}, attributes=["open", "hidden"]),
            ".system": index("system", {"when": date()}, attributes=["open", "hidden", "system"]),
            name(case=2): index(properties={"other": date()}, attributes=["closed"]),
        }
        client = FakeClient(definitions)
        result = self.run_discovery(client, apply=True)
        self.assertEqual(set(result["artifacts"]), {"mft"})
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])
        requested = [unquote(urlsplit(path).path) for _, path, _ in client.calls[1:]]
        self.assertFalse(any(path.startswith("/.system/") or path.startswith("/" + name(case=2) + "/") for path in requested))

    def test_missing_identity_is_an_error_even_when_name_matches(self):
        client = FakeClient({name(): index(None, {"when": date()})})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(any(path.endswith("/_mapping") for _, path, _ in client.calls))

    def test_http_error_after_valid_artifact_prevents_all_writes(self):
        self.write_yaml("forensic_all_dates: [legacy]\n")
        before = (self.directory / "mft.yaml").read_bytes()
        client = FakeClient({name(): index(properties={"when": date()}),
                             name("prefetch"): index("prefetch", {"run": date()}, identity_error="HTTP 403")})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual((self.directory / "mft.yaml").read_bytes(), before)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"mft.yaml"})

    def test_partial_search_or_timeout_prevents_writes_and_closes_latest_pit(self):
        for extra in [{"timed_out": True}, {"_shards": {"failed": 1}}]:
            with self.subTest(extra=extra):
                client = FakeClient({name(): index(properties={"when": date(MIXED_FORMAT)},
                    pages=[[{"when": "2026-10-12"}]], search_extra=extra)})
                with self.assertRaises(discovery.Error):
                    self.run_discovery(client, apply=True)
                self.assertEqual(list(self.directory.iterdir()), [])
                self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-2"}))

    def test_http_scan_error_closes_last_known_pit(self):
        client = FakeClient({name(): index(properties={"when": date(MIXED_FORMAT)}, scan_error="HTTP 500")})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-1"}))

    def test_partial_pit_creation_prevents_writes_and_releases_pit(self):
        client = FakeClient({name(): index(properties={"when": date(MIXED_FORMAT)},
            pit_extra={"_shards": {"failed": 1}})})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-1"}))

    def test_pit_cleanup_error_prevents_writes_without_hiding_scan_error(self):
        for scan_error in [None, "scan failed first"]:
            with self.subTest(scan_error=scan_error):
                client = FakeClient({name(): index(properties={"when": date(MIXED_FORMAT)},
                    pages=[[{"when": "2026-10-12"}]], cleanup_error="cleanup failed", scan_error=scan_error)})
                with self.assertRaisesRegex(discovery.Error, scan_error or "cleanup failed"):
                    self.run_discovery(client, apply=True)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_yaml_prevents_other_valid_artifact_write(self):
        invalid = self.write_yaml("forensic_all_dates: [\n", source="prefetch")
        client = FakeClient({name(): index(properties={"when": date()}),
                             name("prefetch"): index("prefetch", {"when": date()})})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"prefetch.yaml"})
        self.assertEqual(invalid.read_text(), "forensic_all_dates: [\n")

    def test_artifact_identity_cannot_escape_directory(self):
        for source in ["../outside", "/tmp/outside", "sub/artifact", "", ["mft"]]:
            with self.subTest(source=source):
                with self.assertRaises(discovery.Error):
                    self.run_discovery(FakeClient({name(): index(source, {"when": date()})}), apply=True)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_symlink_to_external_yaml_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "target.yaml"
            outside.write_text("forensic_all_dates: [legacy]\n")
            (self.directory / "mft.yaml").symlink_to(outside)
            with self.assertRaises(discovery.Error):
                self.run_discovery(FakeClient({name(): index(properties={"when": date()})}), apply=True)
            self.assertTrue((self.directory / "mft.yaml").is_symlink())
            self.assertEqual(outside.read_text(), "forensic_all_dates: [legacy]\n")

    def test_optional_generation_and_full_artifact_name_are_recognized(self):
        source = "custom_generic_forensic_sqlitehunter_chromium_browser_history_visits"
        for generation in [None, "1", "000001"]:
            with self.subTest(generation=generation):
                concrete = name(source, generation=generation)
                client = FakeClient({concrete: index(source, {"visited": date()})})
                result = self.run_discovery(client)
                self.assertEqual(set(result["artifacts"]), {source})
                self.assertEqual(result["artifacts"][source]["representative_index"], concrete)
                self.assertEqual(result["artifacts"][source]["fields"], ["visited"])

    def test_duplicate_generations_are_not_queried(self):
        representative = name(generation=None)
        client = FakeClient({name(generation="000002"): index(properties={"ignored": date()}),
                             representative: index(properties={"when": date()}),
                             name(generation="000001"): index(properties={"ignored": date()})})
        result = self.run_discovery(client)
        self.assertEqual(result["artifacts"]["mft"]["representative_index"], representative)
        self.assertEqual(result["artifacts"]["mft"]["fields"], ["when"])
        for method, path, _ in client.calls[1:]:
            self.assertIn("/" + representative + "/", path)
        self.assertEqual(result["progress"]["duplicate_indices"], 2)

    def test_empty_first_index_uses_next_nonempty_before_loading_mapping(self):
        client = FakeClient({name(case=3): index(properties={"ignored": date()}),
                             name(case=1): index(empty=True),
                             name(case=2): index(properties={"when": date()})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(result["artifacts"]["mft"]["representative_index"], name(case=2))
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])
        routes = [unquote(urlsplit(path).path) for _, path, _ in client.calls]
        self.assertNotIn("/" + name(case=1) + "/_mapping", routes)
        self.assertNotIn("/" + name(case=3) + "/_search", routes)
        self.assertLess(routes.index("/" + name(case=2) + "/_search"),
                        routes.index("/" + name(case=2) + "/_mapping"))
        self.assertEqual(result["progress"]["empty_indices"], 1)
        self.assertEqual(result["progress"]["duplicate_indices"], 1)

    def test_all_empty_group_is_checkpointed_without_loading_mapping(self):
        client = FakeClient({name(case=1): index(empty=True), name(case=2): index(empty=True)})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(any(path.endswith("/_mapping") for _, path, _ in client.calls))
        self.assertEqual(result["progress"]["completed_artifacts"], 1)
        self.assertEqual(result["progress"]["empty_indices"], 2)
        resumed = FakeClient({})
        again = self.run_discovery(resumed, apply=True, resume=True)
        self.assertEqual(resumed.calls, [])
        self.assertEqual(again["progress"]["recovered_artifacts"], 1)
        self.assertEqual(again["progress"]["empty_indices"], 2)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_nonmatching_indices_are_reported_without_queries(self):
        client = FakeClient({"unrelated": index(), "ir_data_invalid_velociraptor_mft": index()})
        result = self.run_discovery(client)
        self.assertEqual(result["artifacts"], {})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual({item["index"] for item in result["indices"]}, set(client.indices))
        self.assertTrue(all(item["status"] == "skipped" for item in result["indices"]))

    def test_mismatching_artifact_identity_aborts_before_mapping(self):
        self.run_discovery(FakeClient({name(): index(properties={"when": date()})}))
        client = FakeClient({name("prefetch"): index("mft", {"when": date()})})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertFalse(any(path.endswith("/_mapping") for _, path, _ in client.calls))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_no_valid_dates_leave_existing_yaml_byte_for_byte_unchanged(self):
        original = b"---\n# Keep every byte\nforensic_all_dates: [clock, acquisition_time]\nmappings:\n  - src_field: A\n    dest_field: B\n...\n"
        specs = [index(), index(properties={"clock": date("HH:mm:ss")}),
                 index(properties={"unknown": date(MIXED_FORMAT)}, pages=[[{"unknown": "invalid"}]])]
        for spec in specs:
            with self.subTest(spec=spec):
                path = self.directory / "mft.yaml"
                path.write_bytes(original)
                result = self.run_discovery(FakeClient({name(): spec}), apply=True)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(result["files"][0]["status"], "skipped_no_valid_dates")

    def test_no_valid_dates_do_not_even_parse_invalid_existing_yaml(self):
        path = self.write_yaml("forensic_all_dates: [\n")
        result = self.run_discovery(FakeClient({name(): index()}), apply=True)
        self.assertEqual(path.read_text(), "forensic_all_dates: [\n")
        self.assertEqual(result["files"][0]["status"], "skipped_no_valid_dates")

    def test_completed_no_date_artifact_is_not_rescanned_on_resume(self):
        first = self.run_discovery(FakeClient({name(): index()}), apply=True)
        client = FakeClient({name(): index(properties={"newly_arrived": date()})})
        second = self.run_discovery(client, apply=True, resume=True)
        self.assertEqual(client.calls, [])
        self.assertEqual(second["artifacts"], first["artifacts"])
        self.assertEqual(second["progress"]["skipped_no_valid_dates"], 1)
        self.assertEqual(second["progress"]["recovered_artifacts"], 1)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_resume_keeps_completed_artifact_and_restarts_interrupted_scan(self):
        definitions = {
            name(): index(properties={"when": date(MIXED_FORMAT)}, pages=[[{"when": "2026-10-12"}]]),
            name("prefetch"): index("prefetch", {"run": date(MIXED_FORMAT)},
                pages=[[{"run": "10:20:30"}], [{"run": "2026-10-11"}]], interrupt_after=1),
        }
        interrupted = FakeClient(definitions)
        with self.assertRaises(KeyboardInterrupt):
            self.run_discovery(interrupted, apply=True)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(set(json.loads(self.state_file.read_text())["results"]), {"mft"})
        self.assertEqual(interrupted.calls[-1][0:2], ("DELETE", "/_pit"))
        resumed_definitions = copy.deepcopy(definitions)
        resumed_definitions[name("prefetch")].pop("interrupt_after")
        resumed = FakeClient(resumed_definitions)
        resumed.counter = 100
        result = self.run_discovery(resumed, apply=True, resume=True)
        self.assertFalse(any(name() in path for _, path, _ in resumed.calls))
        self.assertFalse(any("/_resolve/index/" in path for _, path, _ in resumed.calls))
        first_search = resumed.scan_calls()[0]
        self.assertNotIn("search_after", first_search)
        self.assertEqual(first_search["pit"]["id"], "pit-101")
        self.assertEqual(result["progress"]["recovered_artifacts"], 1)
        self.assertEqual(result["scanned_documents"], 3)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])
        self.assertEqual(self.read_yaml("prefetch")["forensic_all_dates"], ["run"])

    def test_http_failure_retains_completed_checkpoint_results_for_resume(self):
        client = FakeClient({name(): index(properties={"when": date()}),
                             name("prefetch"): index("prefetch", identity_error="HTTP 503")})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        state = json.loads(self.state_file.read_text())
        self.assertEqual(set(state["results"]), {"mft"})
        self.assertEqual(list(self.directory.iterdir()), [])
        resumed = FakeClient({name("prefetch"): index("prefetch", {"run": date()})})
        result = self.run_discovery(resumed, resume=True)
        self.assertEqual(result["progress"]["completed_artifacts"], 2)
        self.assertFalse(any(name() in path for _, path, _ in resumed.calls))

    def test_resume_preview_applies_current_yaml_and_ignores_new_inventory(self):
        preview = self.run_discovery(FakeClient({name(): index(properties={"when": date()})}))
        self.assertEqual(preview["files"][0]["status"], "created")
        self.write_yaml("forensic_all_dates: [manual]\ncustom: preserved\n")
        client = FakeClient({name("new_artifact"): index("new_artifact", {"new": date()})})
        applied = self.run_discovery(client, apply=True, resume=True)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.read_yaml(), {"forensic_all_dates": ["manual", "when"], "custom": "preserved"})
        self.assertEqual(set(applied["artifacts"]), {"mft"})
        self.assertFalse((self.directory / "new_artifact.yaml").exists())
        before = (self.directory / "mft.yaml").read_bytes()
        repeated = self.run_discovery(FakeClient({}), apply=True, resume=True)
        self.assertEqual(repeated["files"][0]["status"], "unchanged")
        self.assertEqual((self.directory / "mft.yaml").read_bytes(), before)

    def test_resume_after_partial_apply_replans_without_overwriting_finished_file(self):
        client = FakeClient({name(): index(properties={"when": date()}),
                             name("prefetch"): index("prefetch", {"run": date()})})
        apply_files = discovery.apply_files

        def interrupted_apply(changes):
            apply_files(changes[:1])
            raise KeyboardInterrupt()

        with patch.object(discovery, "apply_files", side_effect=interrupted_apply):
            with self.assertRaises(KeyboardInterrupt):
                self.run_discovery(client, apply=True)
        self.assertTrue((self.directory / "mft.yaml").exists())
        self.assertFalse((self.directory / "prefetch.yaml").exists())
        before = (self.directory / "mft.yaml").read_bytes()
        resumed = FakeClient({})
        result = self.run_discovery(resumed, apply=True, resume=True)
        self.assertEqual(resumed.calls, [])
        self.assertEqual((self.directory / "mft.yaml").read_bytes(), before)
        self.assertEqual(self.read_yaml("prefetch")["forensic_all_dates"], ["run"])
        self.assertEqual([item["status"] for item in result["files"]], ["unchanged", "created"])

    def test_without_resume_new_scan_replaces_previous_inventory(self):
        self.run_discovery(FakeClient({name(): index(properties={"when": date()})}))
        client = FakeClient({name("prefetch"): index("prefetch", {"run": date()})})
        result = self.run_discovery(client)
        self.assertEqual(set(result["artifacts"]), {"prefetch"})
        self.assertTrue(any("/_resolve/index/" in path for _, path, _ in client.calls))
        self.assertEqual(set(json.loads(self.state_file.read_text())["results"]), {"prefetch"})

    def test_missing_malformed_and_wrong_version_resume_states_fail_without_requests(self):
        for content in [None, "{invalid", "[]", '{"version": 999}']:
            with self.subTest(content=content):
                if content is None:
                    self.state_file.unlink(missing_ok=True)
                else:
                    self.state_file.write_text(content)
                client = FakeClient({name(): index()})
                with self.assertRaises(discovery.Error):
                    self.run_discovery(client, resume=True)
                self.assertEqual(client.calls, [])
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_resume_rejects_changed_endpoint_pattern_directory_and_version(self):
        self.run_discovery(FakeClient({name(): index(properties={"when": date()})}))
        other = Path(self.temporary.name) / "other-artifacts"
        other.mkdir()
        for override in [{"url": "http://another.invalid:9200"},
                         {"index_pattern": "ir_data_*"}, {"artifacts_dir": str(other)}]:
            with self.subTest(override=override):
                client = FakeClient({})
                with self.assertRaises(discovery.Error):
                    self.run_discovery(client, resume=True, **override)
                self.assertEqual(client.calls, [])
        state = json.loads(self.state_file.read_text())
        state["version"] += 1
        self.state_file.write_text(json.dumps(state))
        with self.assertRaises(discovery.Error):
            self.run_discovery(FakeClient({}), resume=True)

    def test_checkpoint_omits_credentials_original_values_and_pagination_state(self):
        client = FakeClient({name(): index(properties={"when": date(MIXED_FORMAT)},
                             pages=[[{"when": "2026-10-12"}]])})
        with patch.dict(os.environ, {"ES_API_KEY": "secret-api-key", "ES_PASSWORD": "secret-password"}):
            self.run_discovery(client)
        text = self.state_file.read_text()
        for forbidden in ["secret-api-key", "secret-password", "2026-10-12", "pit-", "search_after"]:
            self.assertNotIn(forbidden, text)
        self.assertEqual({path.name for path in Path(self.temporary.name).iterdir()},
                         {"artifacts", "discovery-state.json"})

    def test_checkpoint_cannot_overwrite_unselected_artifact_yaml_in_preview(self):
        original = "---\nforensic_all_dates: [existing]\n"
        path = self.write_yaml(original)
        client = FakeClient({name("prefetch"): index("prefetch", {"run": date()})})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, state_file=str(path))
        self.assertEqual(path.read_text(), original)
        self.assertEqual(client.calls, [])
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"mft.yaml"})

    def test_progress_counts_representatives_duplicates_empty_and_recovered_groups(self):
        client = FakeClient({
            name(case=1): index(empty=True), name(case=2): index(properties={"when": date()}),
            name(case=3): index(properties={"ignored": date()}),
            name("prefetch"): index("prefetch"),
            name("sam", case=1): index("sam", empty=True),
            name("sam", case=2): index("sam", empty=True),
        })
        result = self.run_discovery(client)
        expected = {"completed_artifacts": 3, "total_artifacts": 3, "percent": 100,
                    "recovered_artifacts": 0, "duplicate_indices": 1,
                    "empty_indices": 3, "skipped_no_valid_dates": 2}
        for key, value in expected.items():
            self.assertEqual(result["progress"][key], value, key)
        resumed = self.run_discovery(FakeClient({}), resume=True)
        expected["recovered_artifacts"] = 3
        for key, value in expected.items():
            self.assertEqual(resumed["progress"][key], value, key)

    def test_progress_log_reports_document_count_without_terminal_control_codes(self):
        client = FakeClient({name(): index(properties={"when": date(MIXED_FORMAT)},
            pages=[[{"when": "10:20:30"}], [{"when": "2026-10-12"}]])})
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch.object(discovery, "monotonic", side_effect=range(0, 10000, 2)):
            discovery.run(client, self.options(progress_every=1))
        output = stdout.getvalue() + stderr.getvalue()
        self.assertIn("Progress", output)
        self.assertIn("scanned_documents", output)
        self.assertIn("unresolved_fields", output)
        self.assertNotIn("\r", output)
        self.assertNotIn("\x1b", output)

    def test_progress_counts_empty_candidates_before_artifact_completes(self):
        client = FakeClient({name(case=1): index(empty=True), name(case=2): index(empty=True),
                             name(case=3): index(properties={"when": date()})})
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            discovery.run(client, self.options())
        progress = [json.loads(line.removeprefix("Progress: "))
                    for line in stderr.getvalue().splitlines() if line.startswith("Progress: ")]
        before_third = next(p for p in progress if p["current_index"] == name(case=3)
                            and p["completed_artifacts"] == 0)
        self.assertEqual(before_third["empty_indices"], 2)

    def test_terminal_progress_keeps_long_index_out_of_updating_line(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        source = "custom_generic_forensic_sqlitehunter_chromium_browser_history_visits"
        client = FakeClient({name(source): index(source, {"when": date(MIXED_FORMAT)},
                             pages=[[{"when": "10:20:30"}], [{"when": "2026-10-12"}]])})
        terminal = Terminal()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(terminal), \
                patch.object(discovery, "monotonic", side_effect=range(0, 10000, 2)):
            discovery.run(client, self.options(progress_every=1))
        output = terminal.getvalue()
        self.assertEqual(output.count("Index: " + name(source)), 1)
        self.assertIn("Artifact: " + source, output)
        self.assertIn("\r\x1b[2K", output)
        for part in output.split("\r\x1b[2K")[1:]:
            self.assertLessEqual(len(part.splitlines()[0]), 119)

    def test_failed_checkpoint_replacement_retains_last_completed_artifact(self):
        client = FakeClient({name(): index(properties={"when": date()}),
                             name("prefetch"): index("prefetch", {"run": date()})})
        replace = discovery.os.replace
        replacements = 0

        def fail_third(source, target):
            nonlocal replacements
            replacements += 1
            if replacements == 3:
                raise OSError("simulated checkpoint write failure")
            return replace(source, target)

        with patch.object(discovery.os, "replace", side_effect=fail_third):
            with self.assertRaises(OSError):
                self.run_discovery(client, apply=True)
        self.assertEqual(set(json.loads(self.state_file.read_text())["results"]), {"mft"})
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(list(self.directory.parent.glob(".artifact-dates-state-*.tmp")), [])
        resumed = self.run_discovery(FakeClient(client.indices), resume=True)
        self.assertEqual(resumed["progress"]["recovered_artifacts"], 1)
        self.assertEqual(resumed["progress"]["completed_artifacts"], 2)

    def test_state_file_environment_and_cli_override(self):
        with patch.dict(os.environ, {"ARTIFACTS_DIR": str(self.directory),
                                     "ARTIFACT_DATES_STATE": str(self.state_file)}):
            defaults = discovery.parse_args([])
            explicit = discovery.parse_args(["--state-file", str(self.directory.parent / "override.json"),
                                             "--resume"])
        self.assertEqual(Path(defaults.state_file), self.state_file)
        self.assertFalse(defaults.resume)
        self.assertEqual(Path(explicit.state_file), self.directory.parent / "override.json")
        self.assertTrue(explicit.resume)
        with patch.dict(os.environ, {"ARTIFACTS_DIR": str(self.directory)}, clear=True):
            fallback = discovery.parse_args([])
        self.assertEqual(Path(fallback.state_file).name, ".discover-artifact-dates.state.json")

    def test_artifacts_directory_environment_and_cli_override(self):
        with tempfile.TemporaryDirectory() as override:
            with patch.dict(os.environ, {"ARTIFACTS_DIR": str(self.directory), "ES_URL": "http://example.invalid:9200"}):
                defaults = discovery.parse_args([])
                options = discovery.parse_args(["--artifacts-dir", override, "--apply", "--index-pattern", "ir_data_*"])
            self.assertEqual(Path(defaults.artifacts_dir), self.directory)
            self.assertFalse(defaults.apply)
            self.assertEqual(defaults.index_pattern, "*")
            self.assertEqual(Path(options.artifacts_dir), Path(override))
            self.assertTrue(options.apply)
            self.assertEqual(options.index_pattern, "ir_data_*")

    def test_missing_or_nonexistent_artifacts_directory_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            for argv in [[], ["--artifacts-dir", str(self.directory / "absent")]]:
                with self.subTest(argv=argv), self.assertRaises((SystemExit, discovery.Error)):
                    discovery.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
