import unittest

from .pt1000 import dynamic_qualification, matches_fixture


def sample(at_ms, temperature):
    return {"guest_us": at_ms * 1000, "fixture_c": temperature}


class Pt1000QualificationTest(unittest.TestCase):
    def test_fixed_fixture_keeps_start_reference_and_tolerance(self):
        self.assertTrue(matches_fixture(30.149, 30))
        self.assertFalse(matches_fixture(30.151, 30))
        self.assertFalse(matches_fixture(34, 30))
        self.assertFalse(matches_fixture(None, 30))

    def test_dynamic_reads_follow_the_sampled_temperature(self):
        result = dynamic_qualification(30, [(6500, 6530, 30.03), (12400, 12450, 34.04)],
                                       [sample(6000, 30), sample(11000, 34)], 13900)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["matched_away_from_start"], 1)
        self.assertEqual(result["last_read"]["match"], {"guest_ms": 11000, "fixture_c": 34})

    def test_stuck_start_temperature_cannot_qualify_even_after_fixture_returns(self):
        result = dynamic_qualification(30, [(12000, 12050, 30.03), (22500, 22550, 30.03)],
                                       [sample(11000, 34), sample(21000, 30)], 23000)
        self.assertFalse(result["verified"], result)
        self.assertEqual(result["reason"], "no_change_proven")

    def test_missing_or_invalid_fixture_evidence_fails(self):
        read = [(11100, 11150, 34.03)]
        for events in ([], [sample(15000, 34)], [sample(6000, 34), sample(11000, None)]):
            with self.subTest(events=events):
                self.assertFalse(dynamic_qualification(30, read, events, 12300)["verified"])
        self.assertFalse(dynamic_qualification(30, [], [sample(11000, 34)], 12300)["verified"])

    def test_bounded_publication_overlap_allows_previous_sample(self):
        events = [sample(6000, 34), sample(11000, 35)]
        result = dynamic_qualification(30, [(11150, 11200, 34.04)], events, 11300)
        self.assertTrue(result["verified"], result)
        result = dynamic_qualification(30, [(11300, 11350, 34.04)], events, 11500)
        self.assertFalse(result["verified"], result)

    def test_stale_sample_and_stale_final_read_fail(self):
        events = [sample(11000, 34)]
        fresh_read = [(12000, 12030, 34.04)]
        expired_sample = [(18100, 18150, 34.04)]
        self.assertFalse(dynamic_qualification(30, expired_sample, events, 18400)["verified"])
        self.assertFalse(dynamic_qualification(30, fresh_read, events, 23000)["verified"])

    def test_missing_final_read_cannot_reuse_previous_valid_evidence(self):
        result = dynamic_qualification(30, [(12000, 12030, 34.04), (17000, 17100, None)],
                                       [sample(11000, 34), sample(16000, 35)], 18000)
        self.assertFalse(result["verified"], result)
        self.assertEqual(result["reason"], "last_read_unmatched")

    def test_wide_http_window_or_regressing_guest_clock_cannot_qualify(self):
        self.assertFalse(dynamic_qualification(30, [(11000, 13000, 34.04)],
                                               [sample(12000, 34)], 13200)["verified"])
        self.assertFalse(dynamic_qualification(30, [(11100, 11150, 34.04)],
                                               [sample(11000, 34), sample(10500, 34)], 11400)["verified"])

    def test_guest_clock_regression_cannot_reuse_previous_dynamic_evidence(self):
        events = [sample(6000, 30), sample(11000, 34)]
        for reads in (
            [(12000, 12025, 34.02), (7000, 7025, 30.01)],
            [(12000, 12025, 34.02), (13000, 7000, 30.01), (7500, 7525, 30.01)],
        ):
            with self.subTest(reads=reads):
                result = dynamic_qualification(30, reads, events, 8000)
                self.assertFalse(result["verified"], result)
                self.assertEqual(result["reason"], "invalid_read_clock")
