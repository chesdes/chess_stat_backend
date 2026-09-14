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

    def test_calculate_info_accepts_chess960_with_castling(self):
        pgn = (
            '[Variant "Chess960"]\n'
            '[SetUp "1"]\n'
            '[FEN "rbnk3r/pppppppp/8/8/8/8/PPPPPPPP/RBNK3R w AHah - 0 1"]\n'
            '\n'
            '1. O-O d5 2. d4'
        )
        analysis = [
            {"info": {"type": "cp", "value": 20}, "best_move": "d1h1"},
            {"info": {"type": "cp", "value": 10}, "best_move": "d7d5"},
            {"info": {"type": "cp", "value": 15}, "best_move": "d2d4"},
        ]
        result = self.analyzer.calculate_info(analysis, pgn)

        self.assertEqual(len(result), 3)
        # 960 castling is king-takes-rook in UCI, like the frontend sends.
        self.assertEqual(result[0]["move"], "d1h1")
        # The custom start skips opening detection without crashing.
        self.assertIn("move_classify", result[0])

    def test_calculate_info_accepts_custom_setup(self):
        pgn = (
            '[SetUp "1"]\n'
            '[FEN "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"]\n'
            '\n'
            '3. Bb5 a6'
        )
        analysis = [
            {"info": {"type": "cp", "value": 30}, "best_move": "f1b5"},
            {"info": {"type": "cp", "value": 25}, "best_move": "a7a6"},
        ]
        result = self.analyzer.calculate_info(analysis, pgn)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["move"], "f1b5")

    def test_opening_sacrifice_does_not_crash_brilliant_check(self):
        # In custom positions the very first move can be a sacrifice, so the
        # brilliant check must not assume previous cp history exists.
        pgn = (
            '[SetUp "1"]\n'
            '[FEN "k1r5/pp6/8/1N6/5Q2/6B1/8/K7 w - - 0 1"]\n'
            '\n'
            '1. Qb8+ Rxb8 2. Nc7#'
        )
        analysis = [
            {"info": {"type": "cp", "value": 50}, "best_move": "f4b8"},
            {"info": {"type": "cp", "value": -100}, "best_move": "c8b8"},
            {"info": {"type": "mate", "value": 1}, "best_move": "b5c7"},
        ]
        result = self.analyzer.calculate_info(analysis, pgn)

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["move"], "f4b8")


if __name__ == "__main__":
    unittest.main()
