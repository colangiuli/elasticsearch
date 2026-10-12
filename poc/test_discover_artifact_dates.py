"""Offline behavior checks for date discovery and local artifact configuration."""

import argparse
import contextlib
import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from ruamel.yaml import YAML

import discover_artifact_dates as discovery


MIXED_FORMAT = "yyyy-MM-dd||HH:mm:ss"


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
            if spec.get("scan_error"):
                raise discovery.Error(spec["scan_error"])
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
        self.directory = Path(self.temporary.name)

    def options(self, apply=False, **overrides):
        return argparse.Namespace(**{
            "artifacts_dir": str(self.directory), "index_pattern": "*", "apply": apply,
            "batch_size": 1000, "progress_every": 10, **overrides})

    def run_discovery(self, client, apply=False):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return discovery.run(client, self.options(apply=apply))

    def read_yaml(self, source="mft"):
        return YAML(typ="safe").load((self.directory / f"{source}.yaml").read_text())

    def write_yaml(self, text, source="mft"):
        path = self.directory / f"{source}.yaml"
        path.write_text(text)
        return path

    def test_union_across_cases_nested_multifields_and_exclusions(self):
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
        client = FakeClient({"case-a-mft": first,
                             "case-b-mft": index(properties={"mft": {"properties": {"modified": date()}}}),
                             "case-a-prefetch": index("prefetch", {"run_time": date()})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(set(result["artifacts"]["mft"]["fields"]),
                         {"mft.created", "mft.modified", "entries.when", "raw_date"})
        self.assertEqual(self.read_yaml()["forensic_all_dates"],
                         ["entries.when", "mft.created", "mft.modified", "raw_date"])
        self.assertEqual(self.read_yaml("prefetch")["forensic_all_dates"], ["run_time"])
        self.assertEqual(client.scan_calls(), [])

    def test_preview_proposes_missing_file_without_writing(self):
        client = FakeClient({"case-mft": index(properties={"created": date()})})
        result = self.run_discovery(client)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(result["files"][0]["status"], "created")
        self.assertEqual(result["files"][0]["added"], ["created"])

    def test_existing_unobserved_field_is_preserved_and_reported_unresolved(self):
        self.write_yaml("forensic_all_dates: [legacy]\n")
        client = FakeClient({"case-mft": index(properties={"when": date()})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["legacy", "when"])
        self.assertEqual(result["files"][0]["unresolved"], ["legacy"])
        self.assertEqual(result["files"][0]["unobserved_existing"], ["legacy"])
        self.assertFalse(result["complete"])

    def test_legacy_excluded_multifield_entries_are_removed(self):
        self.write_yaml("forensic_all_dates: [acquisition_time.as_date, collection_time.as_date, when]\n")
        properties = {"when": date(), "acquisition_time": date(fields={"as_date": date()}),
                      "collection_time": {"type": "keyword", "fields": {"as_date": date()}}}
        result = self.run_discovery(FakeClient({"case-mft": index(properties=properties)}), apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])
        self.assertEqual(set(result["files"][0]["removed"]),
                         {"acquisition_time.as_date", "collection_time.as_date"})

    def test_apply_preserves_comments_order_and_other_sections_without_backups(self):
        self.write_yaml("# Artifact settings\nforensic_all_dates:\n"
                        "  - z_date # Keep original order\n  - acquisition_time\n"
                        "  - collection_time\n  - '@timestamp'\n  - forensic_all_dates\n"
                        "  - z_date\nmappings:\n  - src_field: EntryNumber # Keep this comment\n"
                        "    dest_field: mft.id\n    action: move\n")
        definitions = {"case-mft": index(properties={"a_date": date(), "z_date": date()})}
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

    def test_missing_yaml_is_created_even_when_only_clock_formats_exist(self):
        client = FakeClient({"case-mft": index(properties={
            "clock": date("HH:mm:ss"), "clock_tz": date("strict_time"),
            "quoted_clock": date("'year='HH:mm:ss")})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml(), {"forensic_all_dates": []})
        self.assertEqual(set(result["artifacts"]["mft"]["time_only"]),
                         {"clock", "clock_tz", "quoted_clock"})

    def test_date_in_later_page_confirms_mixed_field_and_closes_latest_pit(self):
        client = FakeClient({"case-mft": index(properties={"when": date(MIXED_FORMAT)},
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
        client = FakeClient({"case-mft": index(properties={
            name: date(MIXED_FORMAT) for name in ["clock", "uncertain", "unseen"]},
            pages=[[{"clock": "01:02:03", "uncertain": "01:02:03", "unseen": None}],
                   [{"clock": None, "uncertain": "not a date"}]])})
        result = self.run_discovery(client, apply=True)
        artifact = result["artifacts"]["mft"]
        self.assertEqual(artifact["time_only"], ["clock"])
        self.assertTrue({"uncertain", "unseen"}.issubset(artifact["unresolved"]))
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["uncertain", "unseen", "legacy"])
        self.assertEqual(len(client.scan_calls()), 3)
        self.assertFalse(result["complete"])

    def test_one_case_date_wins_over_clock_only_other_case(self):
        client = FakeClient({
            "case-a-mft": index(properties={"when": date("HH:mm:ss")}),
            "case-b-mft": index(properties={"when": date("yyyy-MM-dd")})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(result["artifacts"]["mft"]["fields"], ["when"])
        self.assertNotIn("when", result["artifacts"]["mft"]["time_only"])
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])

    def test_unknown_evidence_in_another_case_prevents_clock_removal(self):
        self.write_yaml("forensic_all_dates: [when]\n")
        client = FakeClient({
            "case-a-mft": index(properties={"when": date("HH:mm:ss")}),
            "case-b-mft": index(properties={"when": date(MIXED_FORMAT)}, pages=[[{"when": None}]])})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])
        self.assertIn("when", result["artifacts"]["mft"]["unresolved"])
        self.assertNotIn("when", result["artifacts"]["mft"]["time_only"])

    def test_ambiguous_values_resolve_nested_arrays_dotted_keys_and_multifields(self):
        client = FakeClient({"case-mft": index(properties={
            "rows": {"type": "nested", "properties": {"meta": {"properties": {"when": date(MIXED_FORMAT)}}}},
            "raw": {"type": "keyword", "fields": {"as_date": date(MIXED_FORMAT)}},
            "obj": {"properties": {"when": date(MIXED_FORMAT)}}},
            pages=[[{"rows": [{"meta.when": [None, "2026-10-12"]}],
                     "raw": "2026-10-12", "obj.when": "2026-10-12"}]])})
        self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["obj.when", "raw", "rows.meta.when"])
        self.assertEqual(set(client.scan_calls()[0]["_source"]), {"obj.when", "raw", "rows.meta.when"})

    def test_custom_optional_date_parts_require_actual_calendar_evidence(self):
        client = FakeClient({"case-mft": index(properties={
            "optional": date("[yyyy-MM-dd ]HH:mm:ss"),
            "literal": date("'yyyy-MM-dd 'HH:mm:ss")},
            pages=[[{"optional": "10:30:00"}], [{"optional": "2026-10-12 10:30:00"}]])})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["optional"])
        self.assertIn("literal", result["artifacts"]["mft"]["time_only"])

    def test_epoch_mixed_with_clock_is_recognized_only_when_format_allows_it(self):
        client = FakeClient({"case-mft": index(properties={
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
                result = self.run_discovery(FakeClient({"case-mft": spec}))
                self.assertEqual(result["artifacts"]["mft"]["fields"], [])
                self.assertIn("when", result["artifacts"]["mft"]["unresolved"])
                self.assertFalse(result["complete"])

    def test_hidden_indices_are_scanned_but_system_and_closed_indices_are_skipped(self):
        definitions = {
            ".hidden-mft": index(properties={"when": date()}, attributes=["open", "hidden"]),
            ".system": index("system", {"when": date()}, attributes=["open", "hidden", "system"]),
            "closed-mft": index(properties={"other": date()}, attributes=["closed"]),
        }
        client = FakeClient(definitions)
        result = self.run_discovery(client, apply=True)
        self.assertEqual(set(result["artifacts"]), {"mft"})
        self.assertEqual(self.read_yaml()["forensic_all_dates"], ["when"])
        requested = [unquote(urlsplit(path).path) for _, path, _ in client.calls[1:]]
        self.assertFalse(any(path.startswith("/.system/") or path.startswith("/closed-mft/") for path in requested))

    def test_empty_indices_and_missing_identity_do_not_guess_from_index_name(self):
        client = FakeClient({"case-mft": index(properties={"when": date()}, empty=True),
                             "case-prefetch": index(None, {"when": date()})})
        result = self.run_discovery(client, apply=True)
        self.assertEqual(result["artifacts"], {})
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(result["complete"])

    def test_http_error_after_valid_artifact_prevents_all_writes(self):
        self.write_yaml("forensic_all_dates: [legacy]\n")
        before = (self.directory / "mft.yaml").read_bytes()
        client = FakeClient({"case-a": index(properties={"when": date()}),
                             "case-b": index("prefetch", {"run": date()}, identity_error="HTTP 403")})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual((self.directory / "mft.yaml").read_bytes(), before)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"mft.yaml"})

    def test_partial_search_or_timeout_prevents_writes_and_closes_latest_pit(self):
        for extra in [{"timed_out": True}, {"_shards": {"failed": 1}}]:
            with self.subTest(extra=extra):
                client = FakeClient({"case-mft": index(properties={"when": date(MIXED_FORMAT)},
                    pages=[[{"when": "2026-10-12"}]], search_extra=extra)})
                with self.assertRaises(discovery.Error):
                    self.run_discovery(client, apply=True)
                self.assertEqual(list(self.directory.iterdir()), [])
                self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-2"}))

    def test_http_scan_error_closes_last_known_pit(self):
        client = FakeClient({"case-mft": index(properties={"when": date(MIXED_FORMAT)}, scan_error="HTTP 500")})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-1"}))

    def test_partial_pit_creation_prevents_writes_and_releases_pit(self):
        client = FakeClient({"case-mft": index(properties={"when": date(MIXED_FORMAT)},
            pit_extra={"_shards": {"failed": 1}})})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-1"}))

    def test_pit_cleanup_error_prevents_writes_without_hiding_scan_error(self):
        for scan_error in [None, "scan failed first"]:
            with self.subTest(scan_error=scan_error):
                client = FakeClient({"case-mft": index(properties={"when": date(MIXED_FORMAT)},
                    pages=[[{"when": "2026-10-12"}]], cleanup_error="cleanup failed", scan_error=scan_error)})
                with self.assertRaisesRegex(discovery.Error, scan_error or "cleanup failed"):
                    self.run_discovery(client, apply=True)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_yaml_prevents_other_valid_artifact_write(self):
        invalid = self.write_yaml("forensic_all_dates: [\n", source="prefetch")
        client = FakeClient({"case-a": index(properties={"when": date()}),
                             "case-b": index("prefetch", {"when": date()})})
        with self.assertRaises(discovery.Error):
            self.run_discovery(client, apply=True)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"prefetch.yaml"})
        self.assertEqual(invalid.read_text(), "forensic_all_dates: [\n")

    def test_artifact_identity_cannot_escape_directory(self):
        for source in ["../outside", "/tmp/outside", "sub/artifact", "", ["mft"]]:
            with self.subTest(source=source):
                with self.assertRaises(discovery.Error):
                    self.run_discovery(FakeClient({"case-mft": index(source, {"when": date()})}), apply=True)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_symlink_to_external_yaml_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "target.yaml"
            outside.write_text("forensic_all_dates: [legacy]\n")
            (self.directory / "mft.yaml").symlink_to(outside)
            with self.assertRaises(discovery.Error):
                self.run_discovery(FakeClient({"case-mft": index(properties={"when": date()})}), apply=True)
            self.assertTrue((self.directory / "mft.yaml").is_symlink())
            self.assertEqual(outside.read_text(), "forensic_all_dates: [legacy]\n")

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
