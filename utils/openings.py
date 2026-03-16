import json
from pathlib import Path
import chess
import chess.pgn
import io
from io import StringIO

ECO_DIR = Path(__file__).resolve().parent / "eco"
ECO_FILES = ["ecoA.json", "ecoB.json", "ecoC.json", "ecoD.json", "ecoE.json"]

def to_uci_list(moves_string: str):
    pgn_text = f"[Event \"?\"]\n[Site \"?\"]\n[Date \"????.??.??\"]\n[Round \"?\"]\n[White \"?\"]\n[Black \"?\"]\n[Result \"*\"]\n\n{moves_string} *"

    game = chess.pgn.read_game(StringIO(pgn_text))
    board = game.board()

    uci_moves = []

    for move in game.mainline_moves():
        uci_moves.append(move.uci())
        board.push(move)

    return uci_moves

def _load_all_openings():
    openings = []

    for filename in ECO_FILES:
        path = ECO_DIR / filename

        data = json.load(open(path, "r", encoding="utf-8"))

        for fen, entry in data.items():
            names = [entry.get("name", "")]
            
            aliases = entry.get("aliases", {})
            for alias_value in aliases.values():
                names.append(alias_value)

            openings.append({
                "name": entry.get("name", ""),
                "all_names": names,
                "moves": to_uci_list(entry.get("moves", "")),
            })

    return openings

OPENINGS = _load_all_openings()

def find_closest_opening(pgn: str):
    game = chess.pgn.read_game(io.StringIO(pgn))
    
    game_moves = []
    for move in game.mainline_moves():
        game_moves.append(move.uci())
    
    best_match = None
    best_len = 0

    for opening in OPENINGS:
        deb_moves = opening["moves"]
        match_len = 0
        for gm, dm in zip(game_moves, deb_moves):
            if gm == dm:
                match_len += 1
            else:
                break
        
        if match_len > best_len:
            best_len = match_len
            best_match = opening

    return best_match