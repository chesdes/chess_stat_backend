import chess
import chess.pgn
from stockfish import Stockfish
from io import StringIO
from .openings import find_closest_opening
import math
import os
import threading

CPU_LIMIT = os.cpu_count()
analysis_semaphore = threading.Semaphore(CPU_LIMIT)

class Analyzer:
    def __init__(self, depth: int = 15):
        self.depth = depth
        self.PV = {
            chess.PAWN: 100,
            chess.KNIGHT: 300,
            chess.BISHOP: 300,
            chess.ROOK: 500,
            chess.QUEEN: 900,
            chess.KING: 0,
        }
    
    def analyze_pgn(self, pgn_text: str) -> list[dict]:
        with analysis_semaphore:
            results = []
            engine = Stockfish(
                path="/usr/games/stockfish",
                depth=self.depth,
                parameters={"Threads": 1}
            )
            game = chess.pgn.read_game(StringIO(pgn_text))
            if not game:
                raise ValueError("Failed to parse PGN text.")
            board = game.board()
            engine.set_fen_position(board.fen())
            for move in game.mainline_moves():
                board.push(move)
                fen = board.fen()
                best_move = engine.get_best_move()
                engine.set_fen_position(fen)
                info = engine.get_evaluation() or {"type": "cp", "value": 0}
                results.append({
                    "info": info,
                    "best_move": best_move,
                })
            return results

    def calculate_info(self, analyze: list[dict], pgn_text: str) -> list[dict]:
        game = chess.pgn.read_game(StringIO(pgn_text))
        if not game:
            raise ValueError("Failed to parse PGN text.")

        try:
            whiteElo = int(game.headers.get("WhiteElo", 0) or 0)
            blackElo = int(game.headers.get("BlackElo", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("PGN ratings must be integers.") from exc

        moves = list(game.mainline_moves())
        if not moves:
            raise ValueError("PGN must contain at least one move.")
        if len(analyze) != len(moves):
            raise ValueError("Analysis result count must match the number of PGN moves.")

        if game.board() == chess.Board():
            last_win_precent = 50
            opening = find_closest_opening(pgn_text)
        else:
            last_win_precent = None
            opening = {"moves": []}
        board = game.board()
        results = []
        last_expected = None
        accuracy_list = [[100.0], [100.0]]
        last_chesscom_classify = None
        last_diff = None
        move_index = 0
        cps = []
        for move in moves:
            uci = move.uci()
            is_sacrifice = self._is_sacrifice(move, board)
            board.push(move)
            fen = board.fen()
            best_move = analyze[move_index]["best_move"]
            info = analyze[move_index]["info"]
            cpawns = info["value"] if info["type"] == "cp" else info["value"]*1000
            evaluation = self._format_evaluation(info, board.turn)
            expected_score = self._eval_to_expected_score(evaluation)
            
            if info["value"] != 0:
                diff = abs(last_expected - expected_score) if last_expected is not None else 0 
            else:
                diff = 0

            chess_stat_classify = self._get_chess_stat_move_classifies(diff, move_index, uci, opening, 
                                                                       best_move, last_diff)
            chesscom_classify = self._get_chesscom_move_classifies(
                                                                diff, move_index, uci, opening, best_move,
                                                                last_chesscom_classify, whiteElo, blackElo, cps, 
                                                                cpawns, is_sacrifice
                                                                )
            
            win_precent = self._get_win_procentage_in_position(cpawns)
            if info["type"] == "mate" and info["value"] == 0:
                accuracy = 100.0
            else:
                accuracy = self._get_accuracy_procentage(last_win_precent if last_win_precent else win_precent, win_precent)
            if not (move_index < len(opening["moves"]) and uci == opening["moves"][move_index]):
                accuracy_list[move_index % 2].append(accuracy)
            
            average_accuracy = sum(accuracy_list[move_index % 2]) / len(accuracy_list[move_index % 2])

            results.append({
                "fen": fen,
                "move": uci,
                "best_move": best_move,
                "evaluation": evaluation,
                "expected_score": expected_score,
                "accuracy": round(average_accuracy, 1),
                "diff_expected": diff,
                "move_classify": {
                    "chesscom": chesscom_classify,
                    "chess_stat": chess_stat_classify
                }
            })

            last_win_precent = win_precent
            last_chesscom_classify = chesscom_classify
            last_diff = diff
            last_expected = expected_score
            cps.append(info['value']) if info['type'] != "mate" else cps.append(cps[-2]) if len(cps) >= 2 else cps.append(cpawns)
            move_index += 1

        return results
    
    def _get_chess_stat_move_classifies(self, diff: float, move_index: int, 
                                      uci: str, opening: list[str], best_move: str,
                                      last_diff: float):
        if uci == best_move or diff <= 0.04:
            move_classify = "advance"
        elif diff <= 0.12:
            move_classify = "steady"
        else:
            if last_diff and last_diff > 0.12 and diff >= 0.09:
                move_classify = "miss"
            else:
                move_classify = "retreat"

        if (move_index < len(opening["moves"]) and uci == opening["moves"][move_index]):
            move_classify = "theory"

        return move_classify


    def _get_chesscom_move_classifies(self, diff: float, move_index: int, 
                                      uci: str, opening: list[str], best_move: str,
                                      last_classify: str, whiteElo: int, blackElo: int, 
                                      all_cpawns: list[int], cpawns: int, is_sacrifice) -> str:
        elo = whiteElo if move_index % 2 == 0 else blackElo
        bonus = (4000 - elo) / 4000 * 0.05

        if diff == 0 or uci == best_move:
            move_classify = "best"
        elif diff <= 0.02+bonus:
            move_classify = "excellent"
        elif diff <= 0.05+bonus:
            move_classify = "good"
        elif diff <= 0.10+bonus:
            move_classify = "inaccuracy"
        elif diff <= 0.20+bonus:
            move_classify = "mistake"
        else:
            move_classify = "blunder"

        if (len(all_cpawns) > 2 and uci == best_move  
            and abs(all_cpawns[-2]-cpawns) > 100+(elo/40)
            and ((all_cpawns[-2] < cpawns and move_index % 2 == 0) 
                or (all_cpawns[-2] > cpawns and move_index % 2 == 1))):
            move_classify = "great"

        if is_sacrifice and uci == best_move and ((all_cpawns[-2] <= cpawns and move_index % 2 == 0) 
                or (all_cpawns[-2] >= cpawns and move_index % 2 == 1)):
            move_classify = "brilliant"

        if last_classify and last_classify == "blunder" and move_classify == "blunder":
            move_classify = "miss"

        if (move_index < len(opening["moves"]) and uci == opening["moves"][move_index]):
            move_classify = "theoretical"
        
        return move_classify

    def _format_evaluation(self, info: dict, turn: chess.Color = chess.WHITE):
        if info["type"] == "cp":
            return round(info["value"] / 100.0, 2)
        if info["type"] == "mate":
            return f"# -{info['value'] }" if info['value'] == 0 and turn else f"# {info['value']}"
        return 0.0
    
    def _eval_to_expected_score(self, st_eval: float | str) -> float:
        if isinstance(st_eval, str) and st_eval.startswith("#"):
            mate_in = int(st_eval.split(" ")[1])
            return 1.0 if mate_in > 0 else 0.0
        return 1 / (1 + 10 ** (-float(st_eval) / 4.0))
    
    def _get_win_procentage_in_position(self,cp: int) -> float:
        return 50 + (50*((2/(1+(math.e**(-0.004*cp))))-1))
    
    def _get_accuracy_procentage(self, winPercentBefore: float, winPercentAfter: float):
        return (103.1668 * math.e**(-0.04354*(abs(winPercentBefore-winPercentAfter)))) - 3.1669

    def _capture_chain_eval(self, board: chess.Board, move: chess.Move) -> int:
        square = move.to_square
        material_balance = 0

        board = board.copy()
        side = board.turn

        captured = board.piece_at(square)
        if captured:
            material_balance += self.PV[captured.piece_type]
        board.push(move)

        while True:
            attackers = []

            for from_sq in board.attackers(board.turn, square):
                piece = board.piece_at(from_sq)
                if piece and board.is_legal(chess.Move(from_sq, square)):
                    attackers.append((self.PV[piece.piece_type], from_sq))

            if not attackers:
                break

            attackers.sort()
            _, from_sq = attackers[0]

            captured = board.piece_at(square)
            if not captured:
                break
            
            if side == board.turn:
                material_balance += self.PV[captured.piece_type]
            else:
                material_balance -= self.PV[captured.piece_type]

            board.push(chess.Move(from_sq, square))

        return material_balance
    
    def _is_sacrifice(self, move: chess.Move, board: chess.Board) -> bool:
        attacker = self.PV[board.piece_at(move.from_square).piece_type]
        attacked = self.PV[board.piece_at(move.to_square).piece_type] if board.piece_at(move.to_square) else 0
        return self._capture_chain_eval(board, move) < 0 and attacker > attacked