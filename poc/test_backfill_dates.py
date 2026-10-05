"""Offline checks of the actual POC orchestration and its ES request payloads."""

import argparse
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import unittest
from unittest.mock import patch

import backfill_dates as poc


MAPPING = {"properties": {"@timestamp": {"type": "date"}, "modified": {"type": "date"}}}


def iso(millis):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(millis))).isoformat()


def hit(identifier, first, second, **extra):
    source = extra["_source"] if "_source" in extra else {
        "@timestamp": [iso(value) for value in first], "modified": [iso(value) for value in second]}
    return {"_index": "dev-mft", "_id": identifier, "_seq_no": 7,
            "_primary_term": 2, "sort": [identifier],
            "_source": source,
            "fields": {"@timestamp": first, "modified": second}, **extra}


class FakeClient:
    def __init__(self, pages=None, fail_bulk=False, fail_search=False, fail_refresh=False):
        self.pages = pages if pages is not None else [
            [hit("a", ["1000"], ["2000", "1000"], _routing="host-1")],
            [hit("b", [], []), hit("c", ["3000"], [], _source={"forensic_all_dates": []})],
            []]
        self.calls = []
        self.search_count = 0
        self.fail_bulk = fail_bulk
        self.fail_search = fail_search
        self.fail_refresh = fail_refresh

    def request(self, method, path, body=None, ndjson=False):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith("/_mapping"):
            return {"dev-mft": {"mappings": copy.deepcopy(MAPPING)}} if method == "GET" else {"acknowledged": True}
        if "/_settings?" in path:
            return {"dev-mft": {"settings": {"index.mapping.source.mode": "stored"}}}
        if path.endswith("/_count"):
            return {"count": 3, "_shards": {"failed": 0}}
        if "/_pit?" in path:
            return {"id": "pit-0"}
        if path.startswith("/_search?"):
            self.search_count += 1
            return {"pit_id": f"pit-{self.search_count}",
                    "timed_out": self.fail_search, "_shards": {"failed": 0},
                    "hits": {"hits": self.pages.pop(0)}}
        if "/_bulk?" in path:
            assert ndjson and body.endswith("\n")
            actions = [json.loads(line) for line in body.splitlines()][::2]
            items = []
            for i, _ in enumerate(actions):
                if self.fail_bulk and i == len(actions) - 1:
                    items.append({"update": {"status": 409, "error": {"type": "version_conflict_engine_exception"}}})
                else:
                    items.append({"update": {"status": 200, "result": "updated"}})
            return {"errors": self.fail_bulk, "items": items}
        if method == "DELETE" and path == "/_pit":
            return {"succeeded": True}
        if path.endswith("/_refresh"):
            return {"_shards": {"failed": 1 if self.fail_refresh else 0}}
        raise AssertionError((method, path, body))


def args(apply=False, limit=0, refresh="final", progress_every=10):
    return argparse.Namespace(index="dev-mft", target="forensic_all_dates", fields=None,
                              max_docs=limit, batch_size=1000, apply=apply,
                              refresh=refresh, progress_every=progress_every)


class PocTests(unittest.TestCase):
    def run_quietly(self, client, options):
        with contextlib.redirect_stdout(io.StringIO()):
            return poc.run(client, options)

    def test_preview_reads_pages_and_never_mutates_data(self):
        client = FakeClient()
        stats = self.run_quietly(client, args())
        self.assertEqual(stats["scanned"], 3)
        self.assertEqual(stats["prepared"], 1)
        self.assertEqual(stats["skipped"], 2)
        self.assertFalse(any(method == "PUT" or "/_bulk" in path or path.endswith("/_refresh")
                             for method, path, _ in client.calls))
        searches = [body for _, path, body in client.calls if path.startswith("/_search")]
        self.assertEqual(searches[1]["pit"]["id"], "pit-1")
        self.assertEqual(searches[1]["search_after"], ["a"])
        self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-3"}))

    def test_apply_writes_only_new_field_with_routing_and_concurrency(self):
        client = FakeClient()
        stats = self.run_quietly(client, args(apply=True))
        self.assertEqual(stats["updated"], 1)
        bulk = next(body for _, path, body in client.calls if "/_bulk" in path)
        action, body = [json.loads(line) for line in bulk.splitlines()]
        self.assertEqual(body, {"doc": {"forensic_all_dates": [iso(1000), iso(2000)]}})
        self.assertEqual(action["update"]["routing"], "host-1")
        self.assertEqual(action["update"]["if_seq_no"], 7)
        self.assertEqual(action["update"]["if_primary_term"], 2)
        search = next(body for _, path, body in client.calls if path.startswith("/_search"))
        self.assertTrue(search["seq_no_primary_term"])
        self.assertEqual(search["stored_fields"], ["_routing"])
        self.assertEqual(search["_source"], ["@timestamp", "forensic_all_dates", "modified"])
        self.assertEqual(search["docvalue_fields"][1], {"field": "modified", "format": "epoch_millis"})

    def test_exact_source_strings_survive_precision_offsets_and_deduplication(self):
        original = "2026-03-24T01:09:47.8609249+00:00"
        equivalent = "2026-03-24T02:09:47.8609249+01:00"
        distinct_fraction = "2026-03-24T01:09:47.8609250Z"
        client = FakeClient(pages=[[hit("a", ["1774314587860"], ["1774314587860"],
            _source={"@timestamp": original,
                     "modified": [original, equivalent, distinct_fraction, None]})], []])
        self.run_quietly(client, args(apply=True))
        payload = next(body for _, path, body in client.calls if "/_bulk" in path)
        self.assertEqual(json.loads(payload.splitlines()[1]), {"doc": {
            "forensic_all_dates": [original, equivalent, distinct_fraction]}})

    def test_acquisition_time_is_excluded_without_removing_equal_evidence_dates(self):
        for acquisition_millis in [1000, 2000]:
            acquisition = iso(acquisition_millis)
            client = FakeClient(pages=[[hit("a", ["1000"], [],
                _source={"@timestamp": iso(1000), "acquisition_time": acquisition},
                fields={"@timestamp": ["1000"], "acquisition_time": [str(acquisition_millis)],
                        "acquisition_time.as_date": [str(acquisition_millis)]})], []])
            with self.subTest(acquisition=acquisition), patch.dict(MAPPING["properties"], {
                "acquisition_time": {"type": "date", "fields": {"as_date": {"type": "date"}}}
            }):
                self.run_quietly(client, args(apply=True))
            payload = next(body for _, path, body in client.calls if "/_bulk" in path)
            self.assertEqual(json.loads(payload.splitlines()[1]),
                             {"doc": {"forensic_all_dates": [iso(1000)]}})
            search = next(body for _, path, body in client.calls if path.startswith("/_search"))
            self.assertNotIn("acquisition_time", search["_source"])
            self.assertEqual([field["field"] for field in search["docvalue_fields"]],
                             ["@timestamp", "modified"])

    def test_explicit_fields_cannot_include_acquisition_time_or_its_multifields(self):
        mapping = copy.deepcopy(MAPPING)
        mapping["properties"]["acquisition_time"] = {
            "type": "keyword", "fields": {"as_date": {"type": "date"}}}
        for excluded in ["acquisition_time", "acquisition_time.as_date"]:
            with self.subTest(excluded=excluded), self.assertRaisesRegex(poc.Error, "excluded"):
                poc.select_fields(mapping, "forensic_all_dates", ["@timestamp", excluded])
        fields, _ = poc.select_fields(mapping, "forensic_all_dates", None)
        self.assertEqual(fields, ["@timestamp", "modified"])

    def test_only_acquisition_time_leaves_no_eligible_dates(self):
        with self.assertRaisesRegex(poc.Error, "No eligible"):
            poc.select_fields({"properties": {"acquisition_time": {"type": "date"}}},
                              "forensic_all_dates", None)

    def test_object_paths_dotted_keys_and_multifields_use_original_source(self):
        mapping = {"properties": {
            "mft": {"properties": {"fn_atime": {"type": "date"}}},
            "text_date": {"type": "keyword", "fields": {"as_date": {"type": "date"}}},
            "rows": {"properties": {"meta": {"properties": {"when": {"type": "date"}}}}}}}
        client = FakeClient(pages=[[hit("a", [], [],
            _source={"mft": {"fn_atime": iso(1)}, "mft.fn_atime": iso(2),
                     "text_date": iso(3), "rows": [{"meta.when": [iso(4), None]},
                                                    {"meta": {"when": iso(5)}}],
                     "rows.meta": {"when": iso(6)}, "rows.meta.when": iso(7)},
            fields={"mft.fn_atime": ["1", "2"], "text_date.as_date": ["3"],
                    "rows.meta.when": ["4", "5", "6", "7"]})], []])
        with patch.dict(MAPPING, mapping, clear=True):
            self.run_quietly(client, args(apply=True))
        payload = next(body for _, path, body in client.calls if "/_bulk" in path)
        self.assertEqual(json.loads(payload.splitlines()[1])["doc"]["forensic_all_dates"],
                         [iso(n) for n in [1, 2, 4, 5, 6, 7, 3]])
        search = next(body for _, path, body in client.calls if path.startswith("/_search"))
        self.assertIn("text_date", search["_source"])
        self.assertNotIn("text_date.as_date", search["_source"])

    def test_unsupported_or_inconsistent_source_stops_page_before_bulk(self):
        for source in [{"@timestamp": 1}, {"@timestamp": "1"},
                       {"@timestamp": "01/01/1970"}, {"@timestamp": True},
                       {"@timestamp": "2026-02-30T00:00:00Z"},
                       {"@timestamp": {"unexpected": iso(1000)}},
                       {"@timestamp": None}, {}, None,
                       {"@timestamp": [iso(1000), "malformed"]},
                       {"@timestamp": iso(2000)}]:
            with self.subTest(source=source):
                client = FakeClient(pages=[[hit("good", ["1000"], []),
                                           hit("bad", ["1000"], [], _source=source)]])
                with self.assertRaises(poc.Error):
                    self.run_quietly(client, args(apply=True))
                self.assertFalse(any("/_bulk" in path for _, path, _ in client.calls))
                self.assertEqual(client.calls[-1][0], "DELETE")

    def test_each_field_must_match_indexed_dates_independently(self):
        client = FakeClient(pages=[[hit("a", ["1000"], ["2000"],
            _source={"@timestamp": iso(2000), "modified": iso(1000)})]])
        with self.assertRaisesRegex(poc.Error, "do not match indexed"):
            self.run_quietly(client, args(apply=True))
        self.assertFalse(any("/_bulk" in path for _, path, _ in client.calls))

    def test_unindexed_source_values_are_not_copied(self):
        client = FakeClient(pages=[[hit("a", ["1000"], [],
            _source={"@timestamp": iso(1000), "modified": "ignored malformed value"})], []])
        self.run_quietly(client, args(apply=True))
        payload = next(body for _, path, body in client.calls if "/_bulk" in path)
        self.assertEqual(json.loads(payload.splitlines()[1]),
                         {"doc": {"forensic_all_dates": [iso(1000)]}})

    def test_existing_numeric_target_is_not_rewritten(self):
        client = FakeClient(pages=[[hit("a", ["1000"], [],
            _source={"@timestamp": iso(1000), "forensic_all_dates": [1000]})], []])
        stats = self.run_quietly(client, args(apply=True))
        self.assertEqual(stats["skipped"], 1)
        self.assertFalse(any("/_bulk" in path for _, path, _ in client.calls))

    def test_source_comparison_preserves_pre_epoch_and_submillisecond_instants(self):
        for original, expected in [
            ("2026-03-24T01:09:47.8609249+00:00", 1774314587860),
            ("2026-03-24T02:09:47.8609249+01:00", 1774314587860),
            ("1969-12-31T23:59:59.999999999Z", -1),
            ("1970-01-01", 0), ("1970-01-01T00:00:00", 0),
        ]:
            with self.subTest(original=original):
                self.assertEqual(poc.source_epoch_millis(original, "date"), expected)
        for invalid in ["1970-01-01T00:00:00+00:60", "1970-01-01T00:00:00+19:00"]:
            with self.subTest(invalid=invalid), self.assertRaises(poc.Error):
                poc.source_epoch_millis(invalid, "date")

    def test_synthetic_source_is_rejected_before_writes(self):
        for origin in ["settings", "defaults"]:
            client = FakeClient()
            request = client.request

            def synthetic_settings(method, path, body=None, ndjson=False):
                if "/_settings?" in path:
                    return {"dev-mft": {origin: {"index.mapping.source.mode": "synthetic"}}}
                return request(method, path, body, ndjson)

            client.request = synthetic_settings
            with self.subTest(origin=origin), self.assertRaisesRegex(poc.Error, "Synthetic"):
                self.run_quietly(client, args(apply=True))
            self.assertFalse(any(method == "PUT" or "/_bulk" in path for method, path, _ in client.calls))

    def test_nanos_source_string_is_preserved_in_millisecond_target(self):
        client = FakeClient(pages=[[hit("a", ["1000.999999"], [],
            _source={"@timestamp": "1970-01-01T00:00:01.000999999Z"})], []])
        with patch.dict(MAPPING["properties"]["@timestamp"], {"type": "date_nanos"}):
            self.run_quietly(client, args(apply=True))
        payload = next(body for _, path, body in client.calls if "/_bulk" in path)
        self.assertEqual(json.loads(payload.splitlines()[1]), {"doc": {
            "forensic_all_dates": ["1970-01-01T00:00:01.000999999Z"]}})

    def test_small_limit_is_applied_to_the_search(self):
        client = FakeClient(pages=[[hit("a", ["1000"], [])]])
        self.run_quietly(client, args(limit=1))
        search = next(body for _, path, body in client.calls if path.startswith("/_search"))
        self.assertEqual(search["size"], 1)
        self.assertEqual(client.search_count, 1)

    def test_bulk_partial_failure_stops_and_closes_pit(self):
        client = FakeClient(pages=[[hit("a", ["1"], []), hit("b", ["2"], [])]], fail_bulk=True)
        with self.assertRaisesRegex(poc.Error, "Successful updates remain"):
            self.run_quietly(client, args(apply=True))
        self.assertEqual(client.search_count, 1)
        self.assertEqual(client.calls[-2], ("DELETE", "/_pit", {"id": "pit-1"}))
        self.assertEqual(client.calls[-1], ("POST", "/dev-mft/_refresh", None))

    def test_multiple_bulk_pages_refresh_only_once_after_pit_cleanup(self):
        client = FakeClient(pages=[[hit("a", ["1"], [])], [hit("b", ["2"], [])], []])
        stats = self.run_quietly(client, args(apply=True))
        self.assertEqual(stats["updated"], 2)
        self.assertEqual([path for _, path, _ in client.calls if "/_bulk" in path],
                         ["/dev-mft/_bulk?refresh=false"] * 2)
        self.assertEqual(sum(path.endswith("/_refresh") for _, path, _ in client.calls), 1)
        self.assertEqual(client.calls[-2], ("DELETE", "/_pit", {"id": "pit-3"}))
        self.assertEqual(client.calls[-1], ("POST", "/dev-mft/_refresh", None))

    def test_optional_batch_and_none_refresh_policies(self):
        for policy, expected in [("batch", "true"), ("none", "false")]:
            with self.subTest(policy=policy):
                client = FakeClient()
                self.run_quietly(client, args(apply=True, refresh=policy))
                self.assertIn(("POST", "/dev-mft/_bulk?refresh=" + expected),
                              [(method, path) for method, path, _ in client.calls])
                self.assertFalse(any(path.endswith("/_refresh") for _, path, _ in client.calls))

    def test_apply_without_updates_does_not_refresh(self):
        client = FakeClient(pages=[[hit("a", [], [])], []])
        self.run_quietly(client, args(apply=True))
        self.assertFalse(any(path.endswith("/_refresh") for _, path, _ in client.calls))

    def test_final_refresh_failure_is_reported_with_summary(self):
        client = FakeClient(fail_refresh=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(poc.Error, "Final refresh"):
            poc.run(client, args(apply=True))
        summary = json.loads(next(line.split(": ", 1)[1]
                                  for line in output.getvalue().splitlines() if line.startswith("Summary: ")))
        self.assertEqual(summary["updated"], 1)
        self.assertNotIn("Applied to the selected documents", output.getvalue())

    def test_refresh_failure_preserves_original_bulk_error(self):
        client = FakeClient(fail_bulk=True, fail_refresh=True)
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaisesRegex(poc.Error, "Bulk had failed items"):
            self.run_quietly(client, args(apply=True))
        self.assertIn("Final refresh failed", output.getvalue())

    def test_transport_failure_still_attempts_final_refresh(self):
        client = FakeClient()
        request = client.request

        def fail_bulk_transport(method, path, body=None, ndjson=False):
            if "/_bulk" in path:
                raise poc.Error("Transport failure; a write may have partially completed.")
            return request(method, path, body, ndjson)

        client.request = fail_bulk_transport
        with self.assertRaisesRegex(poc.Error, "Transport failure"):
            self.run_quietly(client, args(apply=True))
        self.assertEqual(client.calls[-1], ("POST", "/dev-mft/_refresh", None))

    def test_malformed_refresh_response_preserves_original_bulk_error(self):
        client = FakeClient(fail_bulk=True)
        request = client.request

        def malformed_refresh(method, path, body=None, ndjson=False):
            if path.endswith("/_refresh"):
                raise ValueError("Invalid JSON response")
            return request(method, path, body, ndjson)

        client.request = malformed_refresh
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaisesRegex(poc.Error, "Bulk had failed items"):
            self.run_quietly(client, args(apply=True))
        self.assertIn("Final refresh failed", output.getvalue())

    def test_progress_and_summary_measure_confirmed_updates(self):
        client = FakeClient(pages=[[hit("a", ["1"], [])]])
        output = io.StringIO()
        with patch.object(poc, "monotonic", side_effect=[0, 11, 11, 12]), contextlib.redirect_stdout(output):
            stats = poc.run(client, args(apply=True, limit=1))
        report = json.loads(next(line.split(": ", 1)[1]
                                 for line in output.getvalue().splitlines() if line.startswith("Progress: ")))
        self.assertEqual(report["updated"], 1)
        self.assertEqual(report["elapsed_seconds"], 11)
        self.assertEqual(stats["elapsed_seconds"], 12)
        self.assertEqual(stats["updated_per_second"], 0.1)

    def test_search_timeout_never_sends_bulk(self):
        client = FakeClient(fail_search=True)
        with self.assertRaisesRegex(poc.Error, "timed out"):
            self.run_quietly(client, args(apply=True))
        self.assertFalse(any("/_bulk" in path for _, path, _ in client.calls))
        self.assertEqual(client.calls[-1][0], "DELETE")

    def test_nanos_are_floored_without_float_rounding(self):
        self.assertEqual(poc.epoch_millis("1790000000123.999999"), 1790000000123)
        self.assertEqual(poc.epoch_millis("-0.1"), -1)
        for invalid in ["NaN", "Infinity", "not a date"]:
            with self.assertRaises(poc.Error):
                poc.epoch_millis(invalid)

    def test_nested_or_disabled_docvalues_require_explicit_supported_subset(self):
        mapping = copy.deepcopy(MAPPING)
        mapping["properties"]["rows"] = {"type": "nested", "properties": {"when": {"type": "date"}}}
        mapping["properties"]["hidden"] = {"type": "date", "doc_values": False}
        with self.assertRaisesRegex(poc.Error, "nested date"):
            poc.select_fields(mapping, "forensic_all_dates", None)
        fields, _ = poc.select_fields(mapping, "forensic_all_dates", ["@timestamp"])
        self.assertEqual(fields, ["@timestamp"])

    def test_target_compatibility_and_runtime_shadowing(self):
        for spec in [{"type": "keyword"}, {"type": "date", "format": "yyyy-MM-dd"},
                     {"type": "date", "format": "epoch_second||strict_date_optional_time"},
                     {"type": "date", "doc_values": False}, {"type": "date", "index": False}]:
            mapping = copy.deepcopy(MAPPING)
            mapping["properties"]["forensic_all_dates"] = spec
            with self.assertRaisesRegex(poc.Error, "incompatible"):
                poc.select_fields(mapping, "forensic_all_dates", None)
        mapping = copy.deepcopy(MAPPING)
        mapping["runtime"] = {"modified": {"type": "date"}}
        with self.assertRaisesRegex(poc.Error, "runtime"):
            poc.select_fields(mapping, "forensic_all_dates", None)

    def test_required_routing_is_not_silently_dropped(self):
        with self.assertRaisesRegex(poc.Error, "Required routing"):
            poc.make_updates([hit("a", ["1000"], [])], ["@timestamp"], "forensic_all_dates", True)


if __name__ == "__main__":
    unittest.main()
