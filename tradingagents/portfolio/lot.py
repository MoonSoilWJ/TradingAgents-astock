"""A-share lot size rules for order normalization."""


def lot_size_for_code(code: str) -> int:
    """Return minimum trading lot (100 main board, 200 STAR/ChiNext)."""
    if code.startswith(("688", "300", "301", "588", "589")):
        return 200
    return 100


def round_down_to_lot(shares: int, lot: int) -> int:
    if shares <= 0 or lot <= 0:
        return 0
    return (shares // lot) * lot
