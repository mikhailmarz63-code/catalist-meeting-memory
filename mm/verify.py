"""Plain code, no LLM: is this quote really in that meeting, word for word?

The graders check citations word for word, so we do the same check first.
Only whitespace is normalised. Case, punctuation and every "um" must match.
A quote must sit inside ONE speaker turn (a quote spanning two turns would
not be contiguous in the transcript file).
"""
import re

_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def find_quote(conn, meeting_id: str, quote: str):
    """Return the turn row containing the quote, or None."""
    q = norm(quote)
    if len(q.split()) < 3:          # too short to be evidence of anything
        return None
    for row in conn.execute(
        "SELECT idx, t, speaker, text FROM turns WHERE meeting_id=? ORDER BY idx", (meeting_id,)
    ):
        if q in norm(row["text"]):
            return row
    return None


def check_citations(conn, citations: list[dict]) -> tuple[list[dict], list[str]]:
    """Split citations into verified ones and human-readable error messages."""
    good, errors = [], []
    for c in citations or []:
        mid, quote = str(c.get("meeting_id", "")), str(c.get("quote", ""))
        hit = find_quote(conn, mid, quote)
        if hit:
            good.append({"meeting_id": mid, "quote": norm(quote)})
        else:
            exists = conn.execute("SELECT 1 FROM meetings WHERE id=?", (mid,)).fetchone()
            why = "not found word for word inside a single turn" if exists else "no such meeting id"
            errors.append(f'{mid}: "{quote[:120]}" -> {why}')
    return good, errors
