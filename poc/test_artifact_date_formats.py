"""Offline date recognizer tests; no Elasticsearch service is required."""

import unittest

from discover_artifact_dates import DateFormat


class DateFormatTests(unittest.TestCase):
    def test_default_formats_and_nanos_require_a_date(self):
        for field_type in ("date", "date_nanos"):
            fmt = DateFormat(field_type=field_type)
            self.assertEqual(fmt.kind, "date")
            for value in ("2026-10-12", "2026-10-12T12:34:56.123456789Z",
                          "2026-10-12T12:34:56+02:00", "2026", "2026-10", 0, -1000):
                with self.subTest(field_type=field_type, value=value):
                    self.assertEqual(fmt.classify(value), "date")
            self.assertEqual(fmt.classify("12:34:56"), "unknown")

    def test_mixed_format_requires_values(self):
        fmt = DateFormat("yyyy-MM-dd||HH:mm:ss")
        self.assertEqual(fmt.kind, "unknown")
        self.assertEqual(fmt.classify("2026-10-12"), "date")
        self.assertEqual(fmt.classify("12:34:56"), "time")
        self.assertEqual(fmt.classify("not a date"), "unknown")

    def test_alternative_order_preserves_numeric_clock_values(self):
        first_time = DateFormat("HHmmss||epoch_millis")
        self.assertEqual(first_time.kind, "unknown")
        self.assertEqual(first_time.classify("123456"), "time")
        self.assertEqual(first_time.classify("1770000000000"), "date")
        self.assertEqual(DateFormat("epoch_millis||HHmmss").classify("123456"), "date")

    def test_unknown_alternative_never_falls_through_to_epoch(self):
        fmt = DateFormat("KKmmss||epoch_millis")
        self.assertEqual(fmt.kind, "unknown")
        self.assertEqual(fmt.unsupported_formats, ["KKmmss"])
        self.assertEqual(fmt.classify("123456"), "unknown")
        self.assertEqual(DateFormat("epoch_millis||KKmmss").classify("123456"), "date")

    def test_lenient_builtin_widths_preserve_alternative_order(self):
        for pattern, value, expected in (("hour||epoch_millis", "1", "time"),
                                         ("hour_minute||epoch_millis", "1:2", "time"),
                                         ("hour_minute_second||epoch_millis", "1:2:3", "time"),
                                         ("date_optional_time||HHmmss", "2026-1-2", "date"),
                                         ("date_hour_minute_second", "2026-1-2T3:4:5", "date")):
            with self.subTest(pattern=pattern, value=value):
                self.assertEqual(DateFormat(pattern).classify(value), expected)
        # Strict/custom widths keep their documented semantics.
        self.assertEqual(DateFormat("strict_hour||epoch_millis").classify("1"), "date")
        self.assertEqual(DateFormat("HH||epoch_millis").classify("1"), "date")

    def test_lenient_builtin_subset_miss_is_not_proof_of_rejection(self):
        # ES lenient ISO parsing permits years outside the Python calendar
        # subset: never reinterpret them through the later clock formatter.
        for value in ("123456", "000000"):
            with self.subTest(value=value):
                self.assertEqual(DateFormat("date_optional_time||HHmmss").classify(value), "unknown")
        # The same conservative rule applies to all non-strict built-ins.
        self.assertEqual(DateFormat("year||HHmmss").classify("123456"), "unknown")
        self.assertEqual(DateFormat("hour||epoch_millis").classify("123456"), "unknown")

    def test_calendar_and_time_components_are_validated(self):
        fmt = DateFormat("yyyy-MM-dd'T'HH:mm:ss||HH:mm:ss")
        for value in ("2026-02-29T12:00:00", "2026-13-01T12:00:00", "2026-04-31T12:00:00",
                      "2026-01-00T12:00:00", "2026-01-01T24:00:00", "12:60:00", "12:00:60"):
            with self.subTest(value=value):
                self.assertEqual(fmt.classify(value), "unknown")
        self.assertEqual(fmt.classify("2024-02-29T23:59:59"), "date")
        self.assertEqual(fmt.classify("23:59:59"), "time")

    def test_common_custom_date_patterns(self):
        for pattern, value in (("dd/MM/yyyy", "12/10/2026"), ("d/M/yyyy", "2/1/2026"),
                               ("yyyyMMdd", "20261012"), ("yyyy-MM", "2026-10"),
                               ("yy/MM/dd", "26/10/12"), ("uuuu-MM-dd", "2026-10-12")):
            with self.subTest(pattern=pattern):
                fmt = DateFormat(pattern)
                self.assertEqual(fmt.kind, "date")
                self.assertEqual(fmt.classify(value), "date")
        self.assertEqual(DateFormat("dd/MM/yyyy").classify("31/11/2026"), "unknown")

    def test_builtin_time_formats(self):
        cases = {"hour": "12", "hour_minute": "12:34", "hour_minute_second": "12:34:56",
                 "hour_minute_second_fraction": "12:34:56.123456",
                 "time": "12:34:56.123Z", "time_no_millis": "12:34:56+02:00",
                 "basic_time": "123456.123+0200", "basic_time_no_millis": "123456Z",
                 "t_time": "T12:34:56.123+0200", "t_time_no_millis": "T12:34:56Z"}
        for name, value in cases.items():
            for prefix in ("", "strict_"):
                with self.subTest(name=prefix + name):
                    fmt = DateFormat(prefix + name)
                    self.assertEqual(fmt.kind, "time")
                    self.assertEqual(fmt.classify(value), "time")

    def test_builtin_date_formats(self):
        cases = {"date": "2026-10-12", "basic_date": "20261012",
                 "date_time": "2026-10-12T12:34:56.123Z",
                 "date_time_no_millis": "2026-10-12T12:34:56+0200",
                 "basic_date_time": "20261012T123456.123+0200",
                 "date_hour_minute_second_fraction": "2026-10-12T12:34:56.123456",
                 "ordinal_date": "2024-366", "basic_ordinal_date": "2024366",
                 "week_date": "2026-W42-1", "basic_week_date": "2026W421",
                 "weekyear_week": "2026-W42", "year": "2026", "year_month": "2026-10"}
        for name, value in cases.items():
            for prefix in ("", "strict_"):
                with self.subTest(name=prefix + name):
                    fmt = DateFormat(prefix + name)
                    self.assertEqual(fmt.kind, "date")
                    self.assertEqual(fmt.classify(value), "date")
        self.assertEqual(DateFormat("ordinal_date").classify("2026-366"), "unknown")
        self.assertEqual(DateFormat("week_date").classify("2021-W53-1"), "unknown")

    def test_quoted_letters_are_not_date_components(self):
        fmt = DateFormat("'year yyyy-MM-dd' HH:mm:ss")
        self.assertEqual(fmt.kind, "time")
        self.assertEqual(fmt.classify("year yyyy-MM-dd 12:34:56"), "time")
        fmt = DateFormat("HH 'o''clock' mm")
        self.assertEqual(fmt.kind, "time")
        self.assertEqual(fmt.classify("12 o'clock 34"), "time")
        self.assertEqual(DateFormat("HH''mm").classify("12'34"), "time")

    def test_optional_date_is_classified_by_actual_presence(self):
        fmt = DateFormat("[yyyy-MM-dd ]HH:mm:ss")
        self.assertEqual(fmt.kind, "unknown")
        self.assertEqual(fmt.classify("12:34:56"), "time")
        self.assertEqual(fmt.classify("2026-10-12 12:34:56"), "date")
        self.assertEqual(fmt.classify("2026-02-30 12:34:56"), "unknown")
        fmt = DateFormat("yyyy-MM-dd['T'HH:mm[:ss[.SSS]][XXX]]")
        self.assertEqual(fmt.kind, "date")
        for value in ("2026-10-12", "2026-10-12T12:34", "2026-10-12T12:34:56.123+02:00"):
            self.assertEqual(fmt.classify(value), "date")

    def test_custom_timezone_width_and_offset_bounds(self):
        for token, value in (("X", "Z"), ("X", "+02"), ("XX", "+0230"),
                             ("XXX", "+02:30"), ("XXXX", "+023045"),
                             ("XXXXX", "+02:30:45"), ("Z", "+0200")):
            with self.subTest(token=token):
                fmt = DateFormat("HH:mm:ss" + token)
                self.assertEqual(fmt.kind, "time")
                self.assertEqual(fmt.classify("12:34:56" + value), "time")
        for value in ("12:34:56+19:00", "12:34:56+18:01", "12:34:56+01:60"):
            self.assertEqual(DateFormat("HH:mm:ssXXX").classify(value), "unknown")
        self.assertEqual(DateFormat("HH:mm:ssxxx").classify("12:34:56Z"), "unknown")

    def test_epoch_zero_negative_fraction_and_limits(self):
        for name in ("epoch_second", "epoch_millis"):
            fmt = DateFormat(name)
            self.assertEqual(fmt.kind, "date")
            for value in (0, "0", -1234, "-1234", 1.5, "1.5"):
                with self.subTest(name=name, value=value):
                    self.assertEqual(fmt.classify(value), "date")
            for value in (True, False, float("inf"), float("nan"), "nan", "inf", "1e10", 10 ** 100):
                self.assertEqual(fmt.classify(value), "unknown")
        self.assertEqual(DateFormat("epoch_millis").classify(str(2 ** 63 - 1)), "date")
        self.assertEqual(DateFormat("epoch_millis").classify(str(2 ** 63)), "unknown")
        self.assertEqual(DateFormat("epoch_second").classify(str(2 ** 63 - 1)), "unknown")

    def test_empty_and_unsupported_values(self):
        fmt = DateFormat("yyyy-MM-dd||HH:mm:ss")
        for value in (None, "", "   ", "\n"):
            self.assertEqual(fmt.classify(value), "empty")
        for value in ([], {}, True, False, " 2026-10-12 "):
            self.assertEqual(fmt.classify(value), "unknown")

    def test_advanced_locale_tokens_are_unresolved(self):
        for pattern in ("dd MMM yyyy", "EEE, dd MMM yyyy", "hh:mm a", "yyyy-MM-dd z",
                        "YYYY-'W'ww-e", "yyyy-Q", "yyyy-MM-dd[", "'unterminated", ""):
            with self.subTest(pattern=pattern):
                fmt = DateFormat(pattern)
                self.assertEqual(fmt.kind, "unknown")
                self.assertEqual(fmt.classify("2026-10-12"), "unknown")
                self.assertTrue(fmt.unsupported_formats)


if __name__ == "__main__":
    unittest.main()
