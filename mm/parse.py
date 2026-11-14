"""Plain code, no LLM: turn a transcript file into turns and search chunks.

Why code: the file format is fixed (timestamp + speaker line, then the words).
A regex reads it perfectly, for free, and can never invent a word.
"""
import re
from pathlib import Path

TURN_HEAD = re.compile(r"^(\d\d:\d\d:\d\d) Speaker ([A-D])\s*$")
MEETING_ID = re.compile(r"^([A-Z]{2}\d{4})([a-d])$")

# Chunks are sized by words, not turns: half the turns in this corpus are just "Yeah." or "Mm-hmm."
CHUNK_TURNS = 40     # hard cap on turns per search chunk
CHUNK_OVERLAP = 2    # turns shared with the next chunk, so nothing is cut in half
MAX_CHUNK_WORDS = 220


def parse_meeting(path: Path) -> dict:
    meeting_id = path.stem
    m = MEETING_ID.match(meeting_id)
    if not m:
        raise ValueError(f"unexpected meeting file name: {path.name}")
    team, letter = m.group(1), m.group(2)
    turns, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        head = TURN_HEAD.match(line)
        if head:
            cur = {"t": head.group(1), "speaker": head.group(2), "lines": []}
            turns.append(cur)
        elif cur is not None and line.strip():
            cur["lines"].append(line.strip())
    out = []
    for t in turns:
        text = " ".join(t["lines"]).strip()
        if text:
            out.append({"idx": len(out), "t": t["t"], "speaker": t["speaker"], "text": text})
    return {
        "id": meeting_id,
        "team": team,
        "ord": "abcd".index(letter) + 1,   # a=1 .. d=4: the timeline order
        "turns": out,
    }


def render_turns(turns: list[dict]) -> str:
    return "\n".join(f"[{t['t']}] {t['speaker']}: {t['text']}" for t in turns)


def chunk_turns(turns: list[dict]) -> list[dict]:
    """Sliding window over turns. A chunk closes at CHUNK_TURNS turns or MAX_CHUNK_WORDS words."""
    chunks, i, n = [], 0, len(turns)
    while i < n:
        j, words = i, 0
        while j < n and (j - i) < CHUNK_TURNS:
            words += len(turns[j]["text"].split())
            j += 1
            if words >= MAX_CHUNK_WORDS:
                break
        window = turns[i:j]
        chunks.append({
            "start_idx": window[0]["idx"], "end_idx": window[-1]["idx"],
            "t_start": window[0]["t"], "t_end": window[-1]["t"],
            "text": render_turns(window),
        })
        if j >= n:
            break
        i = max(j - CHUNK_OVERLAP, i + 1)
    return chunks
