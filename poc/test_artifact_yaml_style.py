"""Regression checks for the artifact YAML editing boundary and presentation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discover_artifact_dates as discovery


def artifact(fields=(), *, time_only=(), unresolved=()):
    return {"fields": list(fields), "time_only": list(time_only),
            "unresolved": list(unresolved), "excluded": sorted(discovery.EXCLUDED)}


class ArtifactYamlStyleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.path = self.directory / "mft.yaml"

    def plan(self, content, fields=("mft.created",)):
        if content is not None:
            self.path.write_bytes(content)
        return discovery.plan_files(self.directory, {"mft": artifact(fields)})[0]

    def test_preserves_list_indentation_and_explicit_start(self):
        original = (b"---\nmappings:\n  - src_field: ActivityID\n"
                    b"    dest_field: w10timeline.activity_id\n    action: move\n")
        change = self.plan(original)
        self.assertEqual(change.updated, original + b"forensic_all_dates:\n  - mft.created\n")

    def test_mapping_and_sequence_indents_are_inferred_independently(self):
        original = (b"metadata:\n    nested:\n        description: retained\n"
                    b"mappings:\n  - src_field: Entry\n    action: move\n")
        change = self.plan(original)
        self.assertEqual(change.updated, original + b"forensic_all_dates:\n  - mft.created\n")

    def test_nested_mappings_keep_two_spaces_with_indented_sequence(self):
        original = (b"metadata:\n  nested:\n    description: retained\n"
                    b"mappings:\n  - src_field: Entry\n    action: move\n")
        change = self.plan(original)
        self.assertEqual(change.updated, original + b"forensic_all_dates:\n  - mft.created\n")

    def test_keeps_existing_indentless_sequences(self):
        original = b"mappings:\n- src_field: Entry\n  action: move\n"
        self.assertEqual(self.plan(original).updated,
                         original + b"forensic_all_dates:\n- mft.created\n")

    def test_preserves_markers_comments_quotes_and_crlf(self):
        original = (b"# file comment\r\n---\r\n# mapping comment\r\nmappings:\r\n"
                    b'  - src_field: "Entry"  # item comment\r\n'
                    b"    action: move\r\n...\r\n")
        expected = original[:-5] + b"forensic_all_dates:\r\n  - mft.created\r\n...\r\n"
        self.assertEqual(self.plan(original).updated, expected)

    def test_missing_file_uses_two_space_dash_offset(self):
        self.assertEqual(self.plan(None).updated,
                         b"forensic_all_dates:\n  - mft.created\n")

    def test_preserves_comments_after_explicit_end_marker(self):
        original = b"---\nmappings: []\n... # end comment\n# trailing comment\n"
        expected = (b"---\nmappings: []\nforensic_all_dates:\n  - mft.created\n"
                    b"... # end comment\n# trailing comment\n")
        self.assertEqual(self.plan(original).updated, expected)

    def test_unchanged_values_leave_entire_file_byte_identical(self):
        original = b"---\nforensic_all_dates: [mft.created]\n...\n"
        change = self.plan(original)
        self.assertEqual(change.summary["status"], "unchanged")
        self.assertEqual(change.updated, original)

    def test_second_plan_is_idempotent(self):
        first = self.plan(b"---\nmappings:\n  - action: move\n")
        self.path.write_bytes(first.updated)
        second = discovery.plan_files(self.directory, {"mft": artifact(["mft.created"])})[0]
        self.assertEqual(second.summary["status"], "unchanged")
        self.assertEqual(second.updated, first.updated)

    def test_no_dates_does_not_open_or_validate_invalid_existing_yaml(self):
        original = b"forensic_all_dates: [acquisition_time\n  broken yaml\xff"
        self.path.write_bytes(original)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read")):
            change = discovery.plan_files(self.directory, {"mft": artifact()})[0]
        self.assertEqual(change.summary["status"], "skipped_no_valid_dates")
        self.assertEqual(change.summary["removed"], [])
        self.assertIsNone(change.original)
        self.assertIsNone(change.updated)
        self.assertEqual(self.path.read_bytes(), original)

    def test_no_dates_does_not_plan_creation(self):
        change = self.plan(None, fields=())
        self.assertFalse(self.path.exists())
        self.assertEqual(change.summary["status"], "skipped_no_valid_dates")
        self.assertIsNone(change.updated)

    def test_skip_reasons_distinguish_unknown_times_and_absent_candidates(self):
        cases = [(artifact(), "no_candidates"),
                 (artifact(time_only=["mft.clock"]), "only_times"),
                 (artifact(unresolved=["mft.unknown"]), "unresolved")]
        for result, expected in cases:
            with self.subTest(expected=expected):
                change = discovery.plan_files(self.directory, {"mft": result})[0]
                self.assertEqual(change.summary["reason"], expected)


if __name__ == "__main__":
    unittest.main()
