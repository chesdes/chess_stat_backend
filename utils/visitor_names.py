"""Deterministic daily visitor nicknames like "brave-otter".

Derived from the (already daily-salted) ip_hash, so nicknames rotate every
day and cannot be used to track a visitor across days — same privacy model
as ip_hash itself. Used only in the admin panel to group one visitor's
requests when hunting for abuse.
"""

import hashlib

_ADJECTIVES = (
    "brave", "calm", "clever", "drowsy", "eager", "fancy", "gentle", "happy",
    "honest", "jolly", "kind", "lively", "lucky", "nimble", "odd", "proud",
    "quick", "quiet", "rapid", "silly", "sleepy", "smart", "speedy", "sunny",
    "tame", "tidy", "witty", "zany", "bold", "bright", "chill", "daring",
    "deft", "droll", "fair", "fleet", "frisky", "funky", "gritty", "hearty",
    "humble", "husky", "icy", "jaunty", "keen", "loyal", "merry", "nifty",
    "noble", "peppy", "perky", "plucky", "prompt", "rocky", "rogue", "savvy",
    "sharp", "shifty", "sleek", "sly", "snappy", "spicy", "spry", "sturdy",
    "suave", "swift", "tasty", "wary", "wild", "wise", "zippy",
)

_NOUNS = (
    "otter", "fox", "raven", "badger", "falcon", "turtle", "panda", "koala",
    "lynx", "wolf", "bear", "hawk", "eagle", "shark", "whale", "cobra",
    "viper", "gecko", "iguana", "jaguar", "puma", "bison", "moose", "beaver",
    "crane", "heron", "ibis", "jay", "kiwi", "lark", "mole", "newt",
    "ocelot", "orca", "owl", "ox", "parrot", "pelican", "penguin", "perch",
    "pike", "python", "quail", "rook", "salmon", "seal", "stork", "swan",
    "tapir", "toad", "trout", "turkey", "wasp", "weasel", "wombat", "wren",
    "yak", "zebra", "ant", "bee", "rook", "knight", "bishop", "pawn",
    "king", "queen",
)


def visitor_name(ip_hash: str | None) -> str | None:
    """Map an ip_hash to a stable-within-the-day nickname, or None."""
    if not ip_hash:
        return None
    digest = hashlib.sha256(f"visitor-name:{ip_hash}".encode()).digest()
    adj = _ADJECTIVES[digest[0] % len(_ADJECTIVES)]
    noun = _NOUNS[digest[1] % len(_NOUNS)]
    num = digest[2] % 100
    return f"{adj}-{noun}-{num:02d}"
