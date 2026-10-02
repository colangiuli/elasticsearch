"""Offline checks of the actual POC orchestration and its ES request payloads."""

import argparse
import contextlib
import copy
import io
import json
import unittest

import backfill_dates as poc


MAPPING = {"properties": {"@timestamp": {"type": "date"},
                           "modified": {"type": "date", "format": "epoch_second"}}}


def hit(identifier, first, second, **extra):
    return {"_index": "dev-mft", "_id": identifier, "_seq_no": 7,
            "_primary_term": 2, "sort": [identifier], "_source": {},
            "fields": {"@timestamp": first, "modified": second}, **extra}


class FakeClient:
    def __init__(self, pages=None, fail_bulk=False, fail_search=False):
        self.pages = pages if pages is not None else [
            [hit("a", ["1000"], ["2000", "1000"], _routing="host-1")],
            [hit("b", [], []), hit("c", ["3000"], [], _source={"forensic_all_dates": []})],
            []]
        self.calls = []
        self.search_count = 0
        self.fail_bulk = fail_bulk
        self.fail_search = fail_search

    def request(self, method, path, body=None, ndjson=False):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith("/_mapping"):
            return {"dev-mft": {"mappings": copy.deepcopy(MAPPING)}} if method == "GET" else {"acknowledged": True}
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
        raise AssertionError((method, path, body))


def args(apply=False, limit=0):
    return argparse.Namespace(index="dev-mft", target="forensic_all_dates", fields=None,
                              max_docs=limit, batch_size=200, apply=apply)


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
        self.assertFalse(any(method == "PUT" or "/_bulk" in path for method, path, _ in client.calls))
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
        self.assertEqual(body, {"doc": {"forensic_all_dates": [1000, 2000]}})
        self.assertEqual(action["update"]["routing"], "host-1")
        self.assertEqual(action["update"]["if_seq_no"], 7)
        self.assertEqual(action["update"]["if_primary_term"], 2)
        search = next(body for _, path, body in client.calls if path.startswith("/_search"))
        self.assertTrue(search["seq_no_primary_term"])
        self.assertEqual(search["stored_fields"], ["_routing"])
        self.assertEqual(search["docvalue_fields"][1], {"field": "modified", "format": "epoch_millis"})

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
        self.assertEqual(client.calls[-1], ("DELETE", "/_pit", {"id": "pit-1"}))

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
