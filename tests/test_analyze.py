import unittest

from utils.analyze import Analyzer


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = Analyzer()
        self.pgn = '[Event "test"]\n\n1. e4 e5'
        self.analysis = [
            {"info": {"type": "cp", "value": 0}, "best_move": "e2e4"},
            {"info": {"type": "cp", "value": 0}, "best_move": "e7e5"},
        ]

    def test_calculate_info_accepts_pgn_without_elo(self):
        result = self.analyzer.calculate_info(self.analysis, self.pgn)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["move"], "e2e4")

    def test_calculate_info_rejects_mismatched_result_count(self):
        with self.assertRaisesRegex(ValueError, "match"):
            self.analyzer.calculate_info(self.analysis[:1], self.pgn)

    def test_calculate_info_rejects_invalid_pgn(self):
        with self.assertRaisesRegex(ValueError, "PGN must contain"):
            self.analyzer.calculate_info([], "not a PGN")


if __name__ == "__main__":
    unittest.main()
