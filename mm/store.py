"""The memory store: one SQLite file. Plain code, no LLM.

Tables
  meetings, turns      the raw record, exactly as transcribed
  chunks + chunks_fts  search units, keyword index (FTS5 / BM25)
  vectors              one embedding per chunk, for meaning-based search
  roles                Speaker A-D -> job title, per meeting (LLM-extracted)
  records              decisions / actions / disagreements with a status
  llm_log              every model call: tokens, cost, seconds (for the cost report)
"""
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings(id TEXT PRIMARY KEY, team TEXT, ord INTEGER,
  n_turns INTEGER, n_words INTEGER, extracted INTEGER DEFAULT 0, reconciled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS turns(meeting_id TEXT, idx INTEGER, t TEXT, speaker TEXT, text TEXT,
  PRIMARY KEY(meeting_id, idx));
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, meeting_id TEXT, team TEXT, ord INTEGER,
  start_idx INTEGER, end_idx INTEGER, t_start TEXT, t_end TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_team ON chunks(team);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id',
  tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS vectors(chunk_id INTEGER PRIMARY KEY, team TEXT, v BLOB);
CREATE INDEX IF NOT EXISTS vectors_team ON vectors(team);
CREATE TABLE IF NOT EXISTS roles(meeting_id TEXT, speaker TEXT, role TEXT, PRIMARY KEY(meeting_id, speaker));
CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY, meeting_id TEXT, team TEXT, ord INTEGER,
  kind TEXT,            -- decision | action | disagreement
  topic TEXT, statement TEXT, speaker TEXT, owner TEXT, quote TEXT, t TEXT,
  status TEXT,          -- decision: current|superseded  action: open|done  disagreement: open|resolved
  changed_by INTEGER,   -- id of the later record that superseded / closed this one
  changed_in TEXT,      -- meeting id where that happened
  note TEXT);
CREATE INDEX IF NOT EXISTS records_team ON records(team, ord);
CREATE TABLE IF NOT EXISTS llm_log(ts TEXT DEFAULT CURRENT_TIMESTAMP, stage TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, seconds REAL, ok INTEGER);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def add_meeting(conn, meeting: dict, chunks: list[dict]) -> bool:
    """Insert one parsed meeting. Returns False if it was already stored (ingest is incremental)."""
    if conn.execute("SELECT 1 FROM meetings WHERE id=?", (meeting["id"],)).fetchone():
        return False
    turns = meeting["turns"]
    conn.execute("INSERT INTO meetings(id, team, ord, n_turns, n_words) VALUES(?,?,?,?,?)",
                 (meeting["id"], meeting["team"], meeting["ord"], len(turns),
                  sum(len(t["text"].split()) for t in turns)))
    conn.executemany("INSERT INTO turns VALUES(?,?,?,?,?)",
                     [(meeting["id"], t["idx"], t["t"], t["speaker"], t["text"]) for t in turns])
    for c in chunks:
        cur = conn.execute(
            "INSERT INTO chunks(meeting_id, team, ord, start_idx, end_idx, t_start, t_end, text) VALUES(?,?,?,?,?,?,?,?)",
            (meeting["id"], meeting["team"], meeting["ord"], c["start_idx"], c["end_idx"],
             c["t_start"], c["t_end"], c["text"]))
        conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (cur.lastrowid, c["text"]))
    conn.commit()
    return True


# ---------- search ----------

_WORD = re.compile(r"[A-Za-z0-9']+")
_STOP = set("the a an of to in on for and or is was were be been it that this what which who whom did do does "
            "with at by from as their they them team had has have how why when then than about into out up "
            "s t one two".split())


def _fts_query(q: str) -> str:
    words = [w.lower() for w in _WORD.findall(q)]
    words = [w for w in words if w not in _STOP and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def keyword_search(conn, query: str, team=None, meeting_id=None, k=20) -> list[int]:
    fq = _fts_query(query)
    if not fq:
        return []
    sql = ("SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid WHERE chunks_fts MATCH ?")
    args = [fq]
    if team:
        sql += " AND c.team = ?"; args.append(team)
    if meeting_id:
        sql += " AND c.meeting_id = ?"; args.append(meeting_id)
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"; args.append(k)
    return [r[0] for r in conn.execute(sql, args)]


def vector_search(conn, query: str, team=None, meeting_id=None, k=20) -> list[int]:
    import numpy as np
    from . import embed
    sql, args = "SELECT v.chunk_id, v.v FROM vectors v", []
    if meeting_id:
        sql += " JOIN chunks c ON c.id = v.chunk_id WHERE c.meeting_id = ?"; args.append(meeting_id)
    elif team:
        sql += " WHERE v.team = ?"; args.append(team)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return []
    # ponytail: brute-force cosine over the filtered rows. Fine when a team filter leaves ~100s of
    # chunks. For unfiltered search over 10,000 meetings swap this for sqlite-vec / pgvector (ANN).
    ids = np.array([r[0] for r in rows])
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    scores = mat @ embed.embed_query(query)
    return [int(i) for i in ids[np.argsort(-scores)[:k]]]


def hybrid_search(conn, query: str, team=None, meeting_id=None, k=6) -> list[sqlite3.Row]:
    """Keyword + meaning search, merged by reciprocal rank fusion (a chunk ranked high by either wins)."""
    fused: dict[int, float] = {}
    for ranked in (keyword_search(conn, query, team, meeting_id), vector_search(conn, query, team, meeting_id)):
        for rank, cid in enumerate(ranked):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)
    top = sorted(fused, key=fused.get, reverse=True)[:k]
    if not top:
        return []
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(top))})", top)}
    return [rows[c] for c in top]


def read_turns(conn, meeting_id: str, t_from: str, t_to: str, limit=60) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT idx, t, speaker, text FROM turns WHERE meeting_id=? AND t>=? AND t<=? ORDER BY idx LIMIT ?",
        (meeting_id, t_from, t_to, limit)).fetchall()


def roles_for(conn, meeting_id: str) -> dict:
    return {r["speaker"]: r["role"] for r in
            conn.execute("SELECT speaker, role FROM roles WHERE meeting_id=?", (meeting_id,))}


def team_records(conn, team: str, max_ord: int = 4) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM records WHERE team=? AND ord<=? ORDER BY ord, t, id",
                        (team, max_ord)).fetchall()
