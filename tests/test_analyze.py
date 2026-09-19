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

    def test_efficiency_none_without_clocks(self):
        result = self.analyzer.calculate_info(self.analysis, self.pgn)

        self.assertIsNone(result[0]["efficiency"])
        self.assertIsNone(result[0]["eff_white"])
        self.assertIsNone(result[1]["eff_black"])

    def test_efficiency_with_clocks_and_increment(self):
        pgn = (
            '[Event "t"]\n[TimeControl "180+2"]\n\n'
            '1. e4 {[%clk 0:02:58]} e5 {[%clk 0:02:57]}'
        )
        result = self.analyzer.calculate_info(self.analysis, pgn)

        # 180 - 178 + 2 = 4s spent on e4 with diff 0.
        self.assertAlmostEqual(result[0]["move_time"], 4.0)
        self.assertAlmostEqual(result[0]["time_before"], 180.0)
        self.assertGreater(result[0]["efficiency"], 90)
        self.assertLessEqual(result[0]["efficiency"], 100)
        self.assertAlmostEqual(result[0]["eff_white"], result[0]["efficiency"])
        self.assertIsNone(result[0]["eff_black"])
        self.assertIsNotNone(result[1]["eff_black"])

    def test_efficiency_uses_last_clock_without_time_control(self):
        pgn = (
            '[Event "t"]\n\n'
            '1. e4 {[%clk 0:03:00]} e5 {[%clk 0:02:50]} '
            '2. Nf3 {[%clk 0:02:55]} Nc6 {[%clk 0:02:45]}'
        )
        analysis = self.analysis + [
            {"info": {"type": "cp", "value": 10}, "best_move": "g1f3"},
            {"info": {"type": "cp", "value": 5}, "best_move": "b8c6"},
        ]
        result = self.analyzer.calculate_info(analysis, pgn)

        # First moves of each color have no time_before -> None.
        self.assertIsNone(result[0]["efficiency"])
        self.assertIsNone(result[1]["efficiency"])
        # Second moves use the previous clock of the same color.
        self.assertAlmostEqual(result[2]["move_time"], 5.0)
        self.assertIsNotNone(result[2]["efficiency"])
        self.assertIsNotNone(result[3]["efficiency"])
        self.assertGreaterEqual(result[2]["efficiency"], 0)
        self.assertLessEqual(result[2]["efficiency"], 100)

    def test_efficiency_bad_slow_move_scores_lower(self):
        pgn = (
            '[Event "t"]\n[TimeControl "300"]\n\n'
            '1. e4 {[%clk 0:04:59]} e5 {[%clk 0:04:00]}'
        )
        analysis = [
            {"info": {"type": "cp", "value": 20}, "best_move": "e2e4"},
            {"info": {"type": "cp", "value": -400}, "best_move": "e7e5"},
        ]
        result = self.analyzer.calculate_info(analysis, pgn)

        self.assertGreater(result[0]["efficiency"], result[1]["efficiency"])

    def test_parse_time_control(self):
        parse = self.analyzer._parse_time_control
        self.assertEqual(parse("300+2"), (300.0, 2.0))
        self.assertEqual(parse("180"), (180.0, 0.0))
        self.assertEqual(parse("-"), (None, 0.0))
        self.assertEqual(parse("garbage"), (None, 0.0))

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


    def test_calculate_info_accepts_unknown_elo(self):
        # Lichess casual games with Anonymous export WhiteElo "?" — must not 422.
        for white_elo in ("?", "-", "", "???", None):
            header = "" if white_elo is None else f'[WhiteElo "{white_elo}"]\n'
            pgn = f'{header}[BlackElo "1500"]\n\n1. e4 e5'
            result = self.analyzer.calculate_info(self.analysis, pgn)

            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["move"], "e2e4")

    def test_calculate_info_accepts_lichess_chess960_anonymous(self):
        pgn = (
            '[Event "casual variant:chess960 game"]\n'
            '[Site "https://lichess.org/m8XhkJCV"]\n'
            '[Date "2026.09.19"]\n'
            '[Round "-"]\n'
            '[White "Anonymous"]\n'
            '[Black "chesdes"]\n'
            '[Result "0-1"]\n'
            '[UTCDate "2026.09.19"]\n'
            '[UTCTime "21:58:15"]\n'
            '[WhiteElo "?"]\n'
            '[BlackElo "1500"]\n'
            '[Variant "Chess960"]\n'
            '[TimeControl "-"]\n'
            '[Termination "Normal"]\n'
            '[FEN "bqnbnrkr/pppppppp/8/8/8/8/PPPPPPPP/BQNBNRKR w KQkq - 0 1"]\n'
            '[SetUp "1"]\n'
            '\n'
            '1. g4 c5 2. e3 Qd6 3. f4 b6 4. Ned3 Bxh1 5. Kxh1 Qd5+ 6. Bf3 Qc4 '
            '7. c3 f5 8. Ne5 Qe6 9. c4 Nf6 10. Bd5 Nxd5 11. cxd5 Qxd5+ 12. e4 fxe4 '
            '13. Nb3 e3+ 14. Nf3 e2 15. Nd4 exf1=Q+ 16. Qxf1 Rxf4 17. Ne2 Rxf3 '
            '18. Ng3 Rxf1# 0-1'
        )
        analysis = [
            {"info": {"type": "cp", "value": 20}, "best_move": "g2g4"}
            for _ in range(36)
        ]
        result = self.analyzer.calculate_info(analysis, pgn)

        self.assertEqual(len(result), 36)
        self.assertEqual(result[0]["move"], "g2g4")
        self.assertEqual(result[-1]["move"], "f3f1")
        self.assertIn("move_classify", result[0])


if __name__ == "__main__":
    unittest.main()
