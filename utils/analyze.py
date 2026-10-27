import math
import os
import re
import threading
from io import StringIO

import chess
import chess.pgn
from stockfish import Stockfish

from .openings import find_closest_opening

analysis_semaphore = threading.Semaphore(os.cpu_count() or 1)

_PGN_ESCAPE_RE = re.compile(r"\\(r\n|n|r|t|\"|\\)")
_PGN_ESCAPE_MAP = {"n": "\n", "r": "\n", "r\n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def normalize_pgn(pgn_text: str) -> str:
    if not isinstance(pgn_text, str):
        return pgn_text
    text = pgn_text
    trimmed = text.strip()
    if len(trimmed) >= 2 and trimmed.startswith('"') and trimmed.endswith('"'):
        text = trimmed[1:-1]
    if "\n" in text or not re.search(r'\\(?:n|r|t|")', text):
        return text
    return _PGN_ESCAPE_RE.sub(
        lambda match: _PGN_ESCAPE_MAP.get(match.group(1), match.group(0)),
        text,
    )


def read_game(pgn_text: str) -> chess.pgn.Game:
    game = chess.pgn.read_game(StringIO(pgn_text))
    if not game:
        raise ValueError("Failed to parse PGN text.")
    return game


class Analyzer:
    STOCKFISH_PATH = "/usr/local/bin/stockfish"

    PV = {
        chess.PAWN: 100,
        chess.KNIGHT: 300,
        chess.BISHOP: 300,
        chess.ROOK: 500,
        chess.QUEEN: 900,
        chess.KING: 0,
    }

    EFF_K = 2.0
    EFF_WINDOW_PER_SIDE = 3
    EFF_MAX_TIME_FRAC = 2.0
    ACC_WIN_K = 0.10
    ACC_WIN_CP_SCALE = 0.004
    ACC_MAX_CP = 100_000

    MISS_OPP_GAIN = 0.10
    MISS_TOLERANCE = 0.03
    MISS_MAX_WORSEN = 0.10
    MISS_MIN_ADVANTAGE = 0.60

    MATE_CP = 10_000
    BR_MATE_TRAP_CP = MATE_CP - 1000
    BR_MIN_SACRIFICE_CP = 200
    BR_MAX_LOSS = 0.12
    BR_HOLD_MIN_CP = -100
    BR_ZONE_CP = 400
    BR_WIN_CP = 500
    BR_MIN_LEGAL = 3
    BR_MIN_LEGAL_IN_CHECK = 5
    BR_ACCEPT_GAIN_CP = 600

    def __init__(self, depth: int = 15):
        self.depth = depth

    def analyze_pgn(self, pgn_text: str) -> list[dict]:
        game = read_game(normalize_pgn(pgn_text))
        with analysis_semaphore:
            engine = Stockfish(
                path=self.STOCKFISH_PATH,
                depth=self.depth,
                parameters={"Threads": 1},
            )
            board = game.board()
            engine.set_fen_position(board.fen())
            results = []
            for move in game.mainline_moves():
                board.push(move)
                best_move = engine.get_best_move()
                engine.set_fen_position(board.fen())
                info = engine.get_evaluation() or {"type": "cp", "value": 0}
                results.append({"info": info, "best_move": best_move})
            return results

    def calculate_info(self, analyze: list[dict], pgn_text: str) -> list[dict]:
        pgn_text = normalize_pgn(pgn_text)
        game = read_game(pgn_text)
        elos = (
            self._parse_elo(game.headers.get("WhiteElo", 0)),
            self._parse_elo(game.headers.get("BlackElo", 0)),
        )

        moves = list(game.mainline_moves())
        if not moves:
            raise ValueError("PGN must contain at least one move.")
        if len(analyze) != len(moves):
            raise ValueError("Analysis result count must match the number of PGN moves.")

        time_control = game.headers.get("TimeControl", "")
        correspondence = self._is_correspondence(time_control)
        clocks = self._extract_clocks(game, len(moves))
        base_seconds, increment = self._parse_time_control(time_control)

        standard_start = game.board() == chess.Board()
        if standard_start:
            start_score, last_cp = 0.5, 0
            book = find_closest_opening(pgn_text)["moves"]
        else:
            start_score, last_cp = None, None
            book = []

        board = game.board()
        results = []
        scores = []
        cps = []
        accuracies = ([], [])
        eff_windows = ([], [])
        eff_avg = [None, None]
        last_clock = [None, None]
        last_expected = None

        for index, move in enumerate(moves):
            side = index % 2
            white = side == 0
            uci = move.uci()
            item = analyze[index]
            info = item["info"]
            best_move = item["best_move"]
            is_best = uci == best_move
            is_theory = index < len(book) and uci == book[index]

            if index > 0:
                before_info = analyze[index - 1]["info"]
            else:
                before_info = {"type": "cp", "value": 0} if standard_start else None
            is_brilliant = not is_theory and self._is_brilliant(board, move, item, before_info)

            board.push(move)
            cpawns = info["value"] if info["type"] == "cp" else info["value"] * 1000
            evaluation = self._format_evaluation(info, board.turn)
            expected_score = self._expected_score(info, evaluation, board.turn)

            is_mate_delivered = info["type"] == "mate" and info["value"] == 0
            if not is_mate_delivered and last_expected is not None:
                diff = abs(last_expected - expected_score)
            else:
                diff = 0

            is_miss = (
                self._is_miss(index, scores, expected_score, start_score)
                or self._is_missed_mate(index, analyze, expected_score)
            )
            allows_mate = self._allows_mate(index, analyze)

            chess_stat_classify = self._get_chess_stat_move_classifies(
                diff, is_best, is_theory, is_miss, allows_mate
            )
            chesscom_classify = self._get_chesscom_move_classifies(
                diff, is_best, is_theory, is_brilliant, is_miss, allows_mate,
                elos[side], white, cps, cpawns
            )

            if not is_theory:
                accuracies[side].append(
                    self._get_move_accuracy(cpawns, last_cp, white, is_mate_delivered)
                )
            average_accuracy = self._get_side_accuracy(accuracies[side])

            if correspondence:
                time_before = None
                efficiency, move_time = None, None
            else:
                time_before = last_clock[side] if last_clock[side] is not None else base_seconds
                efficiency, move_time = self._move_efficiency(diff, clocks[index], time_before, increment)
                if efficiency is not None:
                    window = eff_windows[side]
                    window.append(efficiency)
                    del window[:-self.EFF_WINDOW_PER_SIDE]
                    eff_avg[side] = round(sum(window) / len(window), 1)
                if clocks[index] is not None:
                    last_clock[side] = clocks[index]

            results.append({
                "fen": board.fen(),
                "move": uci,
                "best_move": best_move,
                "evaluation": evaluation,
                "expected_score": expected_score,
                "accuracy": round(average_accuracy, 1),
                "diff_expected": diff,
                "efficiency": efficiency,
                "move_time": move_time,
                "time_before": round(time_before, 1) if time_before else None,
                "eff_white": eff_avg[0],
                "eff_black": eff_avg[1],
                "move_classify": {
                    "chesscom": chesscom_classify,
                    "chess_stat": chess_stat_classify,
                },
            })

            last_cp = cpawns
            last_expected = expected_score
            scores.append(expected_score)
            if info["type"] != "mate":
                cps.append(info["value"])
            else:
                cps.append(cps[-2] if len(cps) >= 2 else cpawns)

        return results

    @staticmethod
    def _parse_elo(raw) -> int:
        try:
            return int(float(str(raw).strip()))
        except (ValueError, OverflowError):
            return 0

    @staticmethod
    def _extract_clocks(game, count: int) -> list:
        try:
            clocks = [node.clock() for node in game.mainline()]
        except Exception:
            clocks = []
        clocks = [float(c) if c is not None else None for c in clocks]
        clocks += [None] * (count - len(clocks))
        return clocks[:count]

    @staticmethod
    def _to_float(text) -> float | None:
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_correspondence(raw) -> bool:
        if not raw or not isinstance(raw, str):
            return False
        text = raw.strip().lower()
        if text == "daily" or "day" in text:
            return True
        match = re.fullmatch(r"\d+/(\d+)", text)
        return bool(match) and int(match.group(1)) >= 86400

    def _parse_time_control(self, raw) -> tuple:
        if not raw or not isinstance(raw, str):
            return None, 0.0
        text = raw.strip()
        if text in ("-", "*") or self._is_correspondence(text):
            return None, 0.0
        base_part, _, inc_part = text.rsplit("/", 1)[-1].partition("+")
        base = self._to_float(base_part)
        increment = self._to_float(inc_part)
        if base is not None and base < 0:
            base = None
        if increment is None or increment < 0 or math.isnan(increment):
            increment = 0.0
        return base, increment

    def _move_efficiency(self, diff: float, clock_after: float | None,
                         time_before: float | None, increment: float) -> tuple:
        if clock_after is None or time_before is None or time_before <= 0:
            return None, None
        move_time = max(0.0, time_before - clock_after + increment)
        time_frac = min(move_time / time_before, self.EFF_MAX_TIME_FRAC)
        quality = 1.0 - min(max(float(diff), 0.0), 1.0)
        efficiency = 100.0 * quality / (1.0 + self.EFF_K * time_frac)
        return round(efficiency, 1), round(move_time, 1)

    @staticmethod
    def _side_score(score: float, white: bool) -> float:
        return score if white else 1 - score

    @staticmethod
    def _has_mate_for(info: dict | None, white: bool) -> bool:
        if not info or info.get("type") != "mate":
            return False
        value = info.get("value", 0)
        return value != 0 and (value > 0) == white

    def _is_miss(self, index: int, scores: list[float],
                 expected_score: float, start_score: float | None) -> bool:
        if index < 1:
            return False
        before = scores[index - 2] if index >= 2 else start_score
        if before is None:
            return False
        white = index % 2 == 0
        before = self._side_score(before, white)
        after_opponent = self._side_score(scores[index - 1], white)
        after_mine = self._side_score(expected_score, white)
        if after_opponent - before < self.MISS_OPP_GAIN or after_opponent < self.MISS_MIN_ADVANTAGE:
            return False
        return before - after_mine < self.MISS_MAX_WORSEN and after_mine <= before + self.MISS_TOLERANCE

    def _is_missed_mate(self, index: int, analyze: list[dict], expected_score: float) -> bool:
        if index < 1:
            return False
        white = index % 2 == 0
        if not self._has_mate_for(analyze[index - 1]["info"], white):
            return False
        after = analyze[index]["info"]
        if after.get("type") == "mate" and (after.get("value") == 0 or self._has_mate_for(after, white)):
            return False
        return self._side_score(expected_score, white) >= self.MISS_MIN_ADVANTAGE

    def _allows_mate(self, index: int, analyze: list[dict]) -> bool:
        opponent_white = index % 2 == 1
        if not self._has_mate_for(analyze[index]["info"], opponent_white):
            return False
        return index == 0 or not self._has_mate_for(analyze[index - 1]["info"], opponent_white)

    @staticmethod
    def _get_chess_stat_move_classifies(diff: float, is_best: bool, is_theory: bool,
                                        is_miss: bool, allows_mate: bool) -> str:
        if is_best or diff <= 0.04:
            result = "advance"
        elif diff <= 0.12:
            result = "steady"
        else:
            result = "retreat"

        if not is_best:
            if allows_mate:
                result = "retreat"
            if is_miss:
                result = "miss"

        return "theory" if is_theory else result

    @staticmethod
    def _get_chesscom_move_classifies(diff: float, is_best: bool, is_theory: bool,
                                      is_brilliant: bool, is_miss: bool, allows_mate: bool,
                                      elo: int, white: bool, all_cpawns: list[int],
                                      cpawns: int) -> str:
        bonus = (4000 - elo) / 4000 * 0.05

        if diff == 0 or is_best:
            result = "best"
        else:
            result = "blunder"
            for limit, label in ((0.02, "excellent"), (0.05, "good"),
                                 (0.10, "inaccuracy"), (0.20, "mistake")):
                if diff <= limit + bonus:
                    result = label
                    break

        if len(all_cpawns) > 2 and is_best:
            previous = all_cpawns[-2]
            improved = previous < cpawns if white else previous > cpawns
            if improved and abs(previous - cpawns) > 100 + elo / 40:
                result = "great"

        if is_brilliant:
            result = "brilliant"
        elif not is_best:
            if allows_mate:
                result = "blunder"
            if is_miss:
                result = "miss"

        return "theoretical" if is_theory else result

    @staticmethod
    def _format_evaluation(info: dict, turn: chess.Color = chess.WHITE):
        if info["type"] == "cp":
            return round(info["value"] / 100.0, 2)
        if info["type"] == "mate":
            if info["value"] == 0 and turn:
                return "# -0"
            return f"# {info['value']}"
        return 0.0

    @staticmethod
    def _expected_score(info: dict, evaluation: float | str, turn: chess.Color) -> float:
        if info["type"] == "mate":
            value = info["value"]
            return 1.0 if value > 0 or (value == 0 and turn == chess.BLACK) else 0.0
        return 1 / (1 + 10 ** (-float(evaluation) / 4.0))

    def _get_win_percentage(self, cp: float) -> float:
        cp = max(-self.ACC_MAX_CP, min(self.ACC_MAX_CP, cp))
        return 100 / (1 + math.exp(-self.ACC_WIN_CP_SCALE * cp))

    def _get_move_accuracy(self, cp_after: float, cp_before: float | None,
                           white: bool, is_mate_delivered: bool) -> float:
        if is_mate_delivered or cp_before is None:
            return 100.0
        drop = self._get_win_percentage(cp_before) - self._get_win_percentage(cp_after)
        if not white:
            drop = -drop
        if drop <= 1e-9:
            return 100.0
        return max(0.0, 103.1668 * math.exp(-self.ACC_WIN_K * drop) - 3.1669)

    @staticmethod
    def _get_side_accuracy(acc_list: list[float]) -> float:
        return sum(acc_list) / len(acc_list) if acc_list else 100.0

    def _mover_eval(self, info: dict | None, mover: chess.Color):
        if not info:
            return None, None
        kind, value = info.get("type"), info.get("value", 0)
        sign = 1 if mover == chess.WHITE else -1
        if kind == "mate":
            if value == 0:
                return self.MATE_CP, 0
            mate = value * sign
            cp = (self.MATE_CP - abs(value)) * (1 if mate > 0 else -1)
            return cp, mate
        return value * sign, None

    @staticmethod
    def _cp_to_expected(cp: float) -> float:
        return 1 / (1 + 10 ** (-cp / 400.0))

    def _see(self, board: chess.Board, square: chess.Square) -> int:
        captures = list(board.generate_legal_captures(to_mask=chess.BB_SQUARES[square]))
        if not captures:
            return 0
        move = min(captures, key=lambda m: self.PV[board.piece_type_at(m.from_square)])
        if board.is_en_passant(move):
            victim = self.PV[chess.PAWN]
        else:
            victim = self.PV[board.piece_type_at(square)]
        board.push(move)
        gain = victim - self._see(board, square)
        board.pop()
        return max(0, gain)

    def _hanging(self, board: chess.Board) -> dict:
        side = not board.turn
        hanging = {}
        for square in chess.scan_forward(board.occupied_co[side] & ~board.kings):
            gain = self._see(board, square)
            if gain > 0:
                hanging[square] = gain
        return hanging

    def _captured_value(self, board: chess.Board, move: chess.Move) -> int:
        if board.is_en_passant(move):
            gained = self.PV[chess.PAWN]
        else:
            victim = board.piece_type_at(move.to_square)
            gained = self.PV[victim] if victim else 0
        if move.promotion:
            gained += self.PV[move.promotion] - self.PV[chess.PAWN]
        return gained

    def _risk(self, board: chess.Board, move: chess.Move) -> int:
        captured = self._captured_value(board, move)
        board.push(move)
        hanging = self._hanging(board)
        board.pop()
        return max(hanging.values(), default=0) - captured

    def _new_sacrifice(self, board: chess.Board, move: chess.Move) -> int:
        waiting = board.copy(stack=False)
        waiting.turn = not waiting.turn
        waiting.ep_square = None
        before = self._hanging(waiting)
        captured = self._captured_value(board, move)
        board.push(move)
        after = self._hanging(board)
        board.pop()
        newly = max((gain for square, gain in after.items() if before.get(square, 0) < gain),
                    default=0)
        return newly - captured

    def _accept_cp(self, info: dict, mover: chess.Color) -> int | None:
        if info.get("type") == "mate" and info.get("value") == 0:
            return -self.MATE_CP
        return self._mover_eval(info, mover)[0]

    def _ignored_threat(self, board: chess.Board, move: chess.Move, item: dict, after_cp: int) -> bool:
        accept = item.get("accept_info")
        if not accept:
            return False
        accept_cp = self._accept_cp(accept, board.turn)
        if accept_cp is None:
            return False
        mate_trap = accept_cp >= self.BR_MATE_TRAP_CP
        if accept_cp - after_cp < self.BR_ACCEPT_GAIN_CP and not mate_trap:
            return False
        if self._risk(board, move) < self.BR_MIN_SACRIFICE_CP:
            return False
        return any(self._risk(board, other) < self.BR_MIN_SACRIFICE_CP
                   for other in board.legal_moves if other != move)

    def _is_brilliant(self, board: chess.Board, move: chess.Move, item: dict,
                      before_info: dict | None) -> bool:
        mover = board.turn

        legal = board.legal_moves.count()
        if legal < self.BR_MIN_LEGAL or (board.is_check() and legal < self.BR_MIN_LEGAL_IN_CHECK):
            return False

        after_cp, after_mate = self._mover_eval(item.get("info"), mover)
        before_cp, before_mate = self._mover_eval(before_info, mover)
        if after_cp is None or before_cp is None:
            return False

        if self._cp_to_expected(before_cp) - self._cp_to_expected(after_cp) > self.BR_MAX_LOSS:
            return False
        if before_mate is not None and before_mate > 0:
            if after_mate is None or after_mate >= before_mate:
                return False

        if after_cp < self.BR_HOLD_MIN_CP:
            return False

        states = ((before_cp, before_mate), (after_cp, after_mate))

        if any(cp > self.BR_WIN_CP and not (mate is not None and mate > 0) for cp, mate in states):
            accept = item.get("accept_info")
            if not accept or (self._accept_cp(accept, mover) or 0) < self.BR_MATE_TRAP_CP:
                return False
            return self._risk(board, move) >= self.BR_MIN_SACRIFICE_CP

        quiet_in_zone = (
            not board.is_capture(move) and not board.gives_check(move)
            and any(self.BR_ZONE_CP < cp <= self.BR_WIN_CP and not (mate is not None and mate > 0)
                    for cp, mate in states)
        )
        if not quiet_in_zone and self._new_sacrifice(board, move) >= self.BR_MIN_SACRIFICE_CP:
            return True
        return self._ignored_threat(board, move, item, after_cp)