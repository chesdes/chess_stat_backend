from .chesscom import ChessComParser

def get_parser(site: str):
    parsers = {
        "chesscom": ChessComParser(),
    }
    return parsers.get(site, None)

__all__ = ["get_parser", "ChessComParser"]