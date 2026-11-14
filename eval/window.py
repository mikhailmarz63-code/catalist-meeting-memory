"""Helper for writing eval questions by hand: print one meeting's turns in a time window."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import parse
mid, a, b = sys.argv[1], sys.argv[2], sys.argv[3]
width = int(sys.argv[4]) if len(sys.argv) > 4 else 400
p = Path(__file__).resolve().parent.parent / "data" / "transcripts" / (mid + ".txt")
for t in parse.parse_meeting(p)["turns"]:
    if a <= t["t"] <= b and len(t["text"].split()) > 3:
        print(f"{t['t']} {t['speaker']}: {t['text'][:width]}")
