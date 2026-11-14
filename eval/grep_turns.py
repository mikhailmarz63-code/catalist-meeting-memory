"""Helper for writing eval questions by hand: print the turns of one team that match a regex."""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import parse
team, pat = sys.argv[1], re.compile(sys.argv[2], re.I)
width = int(sys.argv[3]) if len(sys.argv) > 3 else 260
for p in sorted((Path(__file__).resolve().parent.parent / "data" / "transcripts").glob(team + "*.txt")):
    for t in parse.parse_meeting(p)["turns"]:
        if pat.search(t["text"]):
            print(f"{p.stem} {t['t']} {t['speaker']}: {t['text'][:width]}")
