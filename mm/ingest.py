"""Ingest = file every meeting into memory. Three stages, each resumable and incremental.

  1. index     [code]  parse, chunk, keyword index, embeddings
  2. extract   [LLM]   one meeting at a time -> roles, decisions, actions, disagreements
                       [code] any record whose quote is not word-for-word in the transcript is dropped
  3. reconcile [LLM]   per team, in meeting order: does this record replace / close an earlier one?
                       [code] writes the status (current, superseded, open, done, resolved)

A new meeting tomorrow costs one extract call and one reconcile call. Nothing old is re-read.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import llm, parse, store, verify

TRANSCRIPTS = store.ROOT / "data" / "transcripts"
SEGMENT_WORDS = 4500   # long meetings are extracted in segments so no call grows with meeting length

# ---------- stage 1: index (code only) ----------


def index_all(conn, folder: Path = TRANSCRIPTS) -> int:
    added = 0
    for path in sorted(folder.glob("*.txt")):
        meeting = parse.parse_meeting(path)
        if store.add_meeting(conn, meeting, parse.chunk_turns(meeting["turns"])):
            added += 1
    embed_missing(conn)
    return added


def embed_missing(conn, batch=256) -> int:
    from . import embed
    rows = conn.execute("SELECT c.id, c.team, c.text FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id "
                        "WHERE v.chunk_id IS NULL").fetchall()
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vecs = embed.embed([r["text"] for r in part])
        conn.executemany("INSERT INTO vectors(chunk_id, team, v) VALUES(?,?,?)",
                         [(r["id"], r["team"], v.tobytes()) for r, v in zip(part, vecs)])
        conn.commit()
        print(f"  embedded {min(i + batch, len(rows))}/{len(rows)} chunks", flush=True)
    return len(rows)


# ---------- stage 2: extract (LLM reads one meeting) ----------

EXTRACT_PROMPT = """This is {part} of meeting {mid}: meeting {ord} of 4 for team {team}, a four-person team
(project manager, industrial designer, user interface designer, marketing) designing a TV remote control.
Speech is raw: false starts, "um", people talking over each other.

Extract what a teammate who missed this meeting must know. Return JSON:
{{
 "roles": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
 "records": [
  {{"kind": "decision" | "action" | "disagreement",
    "topic": "2-4 word label, e.g. 'energy source', 'case material', 'speech recognition', 'target price'",
    "statement": "one plain sentence: what was decided / promised / disputed, with specifics (numbers, materials, names of features)",
    "speaker": "A-D: who proposed it, promised it, or raised the objection",
    "owner": "A-D: for actions, who will do it; else null",
    "quote": "verbatim words copied from ONE speaker turn that best prove it, 6-30 words"}}
 ]
}}

Rules:
- roles: one of "project manager", "industrial designer", "user interface designer", "marketing", or "unknown".
  Use what people say about themselves and what they present. Do not guess from the letter.
- decision: the team settled something, even provisionally ("let's go with rubber"). Include proposals that were
  explicitly rejected or features that were dropped: say "dropped X because Y".
  Include requirements handed down by management (price, budget, constraints).
- action: someone said they will check, find out, or prepare something. Also record when a person REPORTS BACK
  on something they checked earlier (kind "action", statement starts with "Reported: ...").
- disagreement: someone pushed back and it was not settled on the spot. Say who wanted what.
- quote: copy character for character from the transcript, including "uh" and "um". Never join two turns.
  Never paraphrase. If you cannot find a quote, leave the record out.
- topic: name the THING (the feature, material, component, number), the same way each time, so that a later
  record about the same thing can be matched to it. "speech recognition", never "special options".
- statement: name people by letter ("B proposed kinetic; A objected that..."). One record per thing: if a feature
  is cut in a costing exercise, give each cut feature its own record with the cost figures mentioned.
- A disagreement that was settled later in this same transcript is a decision, not a disagreement: record the
  outcome and say who objected.
- Skip meeting logistics and small talk: agendas, room equipment, when to meet next, questionnaires, drawing
  animals, celebrations, opinions nobody acted on.
- 6 to 20 records. Prefer concrete product choices, costs, and who-said-what.

TRANSCRIPT
{transcript}"""


def _segments(turns):
    seg, words = [], 0
    for t in turns:
        seg.append(t)
        words += len(t["text"].split())
        if words >= SEGMENT_WORDS:
            yield seg
            seg, words = [], 0
    if seg:
        yield seg


def extract_meeting(mid: str) -> tuple[str, int, int]:
    """Runs in a worker thread with its own DB connection. Returns (meeting, kept, dropped)."""
    conn = store.connect()
    try:
        m = conn.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        turns = [dict(r) for r in conn.execute(
            "SELECT idx, t, speaker, text FROM turns WHERE meeting_id=? ORDER BY idx", (mid,))]
        segs = list(_segments(turns))
        roles, kept, dropped = {}, 0, 0
        rows = []
        for n, seg in enumerate(segs, 1):
            data = llm.call(EXTRACT_PROMPT.format(
                part=f"part {n} of {len(segs)}", mid=mid, ord=m["ord"], team=m["team"],
                transcript=parse.render_turns(seg)), model=llm.FAST, stage="extract")
            for sp, role in (data.get("roles") or {}).items():
                if sp in "ABCD" and role and role != "unknown":
                    roles.setdefault(sp, str(role).lower())
            for r in data.get("records") or []:
                hit = verify.find_quote(conn, mid, str(r.get("quote", "")))
                if not hit or r.get("kind") not in ("decision", "action", "disagreement"):
                    dropped += 1          # code, not the model, decides what is trustworthy
                    continue
                status = {"decision": "current", "action": "open", "disagreement": "open"}[r["kind"]]
                rows.append((mid, m["team"], m["ord"], r["kind"], str(r.get("topic", ""))[:60],
                             str(r.get("statement", "")), hit["speaker"], r.get("owner"),
                             verify.norm(r["quote"]), hit["t"], status))
                kept += 1
        conn.execute("DELETE FROM records WHERE meeting_id=?", (mid,))
        conn.executemany("INSERT INTO records(meeting_id, team, ord, kind, topic, statement, speaker, owner, quote, t, status) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("DELETE FROM roles WHERE meeting_id=?", (mid,))
        conn.executemany("INSERT INTO roles VALUES(?,?,?)", [(mid, s, r) for s, r in roles.items()])
        conn.execute("UPDATE meetings SET extracted=1 WHERE id=?", (mid,))
        conn.commit()
        return mid, kept, dropped
    finally:
        conn.close()


# ---------- stage 3: reconcile (what is current?) ----------

RECONCILE_PROMPT = """Team {team} is designing a TV remote over four meetings. Below are memory records.
EARLIER = records from meetings before {mid}. NEW = records from meeting {mid} (meeting {ord} of 4).

For each NEW record, decide whether it changes the status of an EARLIER record:
- "supersedes": a NEW decision replaces an EARLIER decision on the same subject (different choice, dropped feature,
  changed number). Not for mere repeats or added detail.
- "completes": a NEW record shows an EARLIER action was done (the person reported back, or the thing was delivered).
- "resolves": a NEW decision settles an EARLIER disagreement.
Only link records about the same subject. When unsure, do not link.

Return JSON: {{"links": [{{"new": <new id>, "earlier": <earlier id>, "relation": "supersedes"|"completes"|"resolves",
"note": "one short sentence: what changed"}}]}}  (empty list if nothing changed)

EARLIER
{earlier}

NEW
{new}"""


def _fmt(rows):
    return "\n".join(f"#{r['id']} [{r['meeting_id']}] {r['kind']} ({r['status']}) | {r['topic']} | {r['statement']}"
                     for r in rows) or "(none)"


def reconcile_team(team: str) -> tuple[str, int]:
    conn = store.connect()
    links = 0
    try:
        todo = conn.execute("SELECT * FROM meetings WHERE team=? AND extracted=1 AND reconciled=0 ORDER BY ord",
                            (team,)).fetchall()
        for m in todo:
            new = conn.execute("SELECT * FROM records WHERE meeting_id=?", (m["id"],)).fetchall()
            # only still-live earlier records can change, which keeps this prompt small however long a team runs
            earlier = conn.execute(
                "SELECT * FROM records WHERE team=? AND ord<? AND status IN ('current','open') ORDER BY ord, id",
                (team, m["ord"])).fetchall()
            if new and earlier:
                data = llm.call(RECONCILE_PROMPT.format(team=team, mid=m["id"], ord=m["ord"],
                                                        earlier=_fmt(earlier), new=_fmt(new)),
                                model=llm.FAST, stage="reconcile")
                new_ids = {r["id"] for r in new}
                old = {r["id"]: r for r in earlier}
                allowed = {("supersedes", "decision"): "superseded", ("completes", "action"): "done",
                           ("resolves", "disagreement"): "resolved"}
                for ln in data.get("links") or []:
                    try:
                        n, e, rel = int(ln["new"]), int(ln["earlier"]), ln["relation"]
                    except (KeyError, TypeError, ValueError):
                        continue
                    # code enforces the rules: ids must exist, relation must fit the record kind
                    if n not in new_ids or e not in old or (rel, old[e]["kind"]) not in allowed:
                        continue
                    conn.execute("UPDATE records SET status=?, changed_by=?, changed_in=?, note=? WHERE id=?",
                                 (allowed[(rel, old[e]["kind"])], n, m["id"], str(ln.get("note", ""))[:300], e))
                    links += 1
            conn.execute("UPDATE meetings SET reconciled=1 WHERE id=?", (m["id"],))
            conn.commit()
        return team, links
    finally:
        conn.close()


# ---------- roles: majority vote (code only) ----------

ROLES = ("project manager", "industrial designer", "user interface designer", "marketing")


def consolidate_roles(conn) -> int:
    """The model guesses roles per meeting and often swaps the two designers. In this corpus a speaker
    letter keeps its role across a team's meetings (the project-manager letter is identical in 34 of 35
    teams), so code takes a vote across the team's meetings and picks the one-role-per-speaker assignment
    with the most votes. Returns the number of meetings whose role map was corrected."""
    from itertools import permutations
    fixed = 0
    for (team,) in conn.execute("SELECT DISTINCT team FROM meetings WHERE extracted=1").fetchall():
        mids = [r[0] for r in conn.execute("SELECT id FROM meetings WHERE team=? ORDER BY ord", (team,))]
        votes = {}
        for mid in mids:
            for sp, role in store.roles_for(conn, mid).items():
                votes[(sp, role)] = votes.get((sp, role), 0) + 1
        best = max(permutations(ROLES), key=lambda p: sum(votes.get((s, r), 0) for s, r in zip("ABCD", p)))
        final = dict(zip("ABCD", best))
        for mid in mids:
            if store.roles_for(conn, mid) != final:
                fixed += 1
                conn.execute("DELETE FROM roles WHERE meeting_id=?", (mid,))
                conn.executemany("INSERT INTO roles VALUES(?,?,?)", [(mid, s, r) for s, r in final.items()])
    conn.commit()
    return fixed


KICKOFF_PROMPT = """Below are the first minutes and a sample of later turns from the kick-off meeting of a
four-person team (Speakers A-D). Exactly one speaker holds each role: project manager, industrial designer,
user interface designer, marketing. Work out who holds which, from how people introduce themselves, how the
project manager addresses them, and what each says they will work on (industrial designer = how the device
works inside, components, materials; user interface designer = buttons, layout, how the user operates it;
marketing = users, market, requirements, trends).
Return JSON: {{"A": role, "B": role, "C": role, "D": role, "sure": true|false}}
"sure" is false if the transcript does not let you separate two of the roles.

TRANSCRIPT
{transcript}"""


def roles_from_kickoff(team: str) -> tuple[str, bool]:
    """The vote above fails when the model swaps the two designers in most of a team's meetings.
    People state their roles in the kick-off, so one small call per TEAM (not per meeting) reads the
    kick-off and, when it is sure, that answer overrides the vote for all of the team's meetings."""
    conn = store.connect()
    try:
        mids = [r[0] for r in conn.execute("SELECT id FROM meetings WHERE team=? ORDER BY ord", (team,))]
        turns = [dict(r) for r in conn.execute(
            "SELECT idx, t, speaker, text FROM turns WHERE meeting_id=? ORDER BY idx", (mids[0],))]
        head, words = [], 0
        for t in turns:
            head.append(t); words += len(t["text"].split())
            if words > 1500:
                break
        data = llm.call(KICKOFF_PROMPT.format(transcript=parse.render_turns(head)), model=llm.FAST, stage="roles")
        got = {s: str(data.get(s, "")).lower() for s in "ABCD"}
        if not data.get("sure") or sorted(got.values()) != sorted(ROLES):
            return team, False
        for mid in mids:
            conn.execute("DELETE FROM roles WHERE meeting_id=?", (mid,))
            conn.executemany("INSERT INTO roles VALUES(?,?,?)", [(mid, s, r) for s, r in got.items()])
        conn.commit()
        return team, True
    finally:
        conn.close()


# ---------- runner ----------


def run(workers: int = 4, only_team: str | None = None):
    conn = store.connect()
    print(f"index: {index_all(conn)} new meetings")
    where, args = "extracted=0", []
    if only_team:
        where += " AND team=?"; args.append(only_team)
    todo = [r["id"] for r in conn.execute(f"SELECT id FROM meetings WHERE {where} ORDER BY id", args)]
    print(f"extract: {len(todo)} meetings to do, {workers} workers")
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(extract_meeting, mid): mid for mid in todo}
        for n, f in enumerate(as_completed(futs), 1):
            try:
                mid, kept, dropped = f.result()
                print(f"  [{n}/{len(todo)}] {mid}: kept {kept}, dropped {dropped} (quote not verbatim)", flush=True)
            except Exception as e:           # one bad meeting must not stop the batch; it stays extracted=0
                print(f"  [{n}/{len(todo)}] {futs[f]}: FAILED {e}", flush=True)
                if "not logged in" in str(e).lower():
                    pool.shutdown(cancel_futures=True)
                    raise
    teams = [r[0] for r in conn.execute(
        "SELECT DISTINCT team FROM meetings WHERE extracted=1 AND reconciled=0" +
        (" AND team=?" if only_team else ""), args)]
    print(f"reconcile: {len(teams)} teams")
    with ThreadPoolExecutor(workers) as pool:
        for f in as_completed([pool.submit(reconcile_team, t) for t in teams]):
            try:
                team, links = f.result()
                print(f"  {team}: {links} status changes", flush=True)
            except Exception as e:
                print(f"  reconcile FAILED: {e}", flush=True)
    print(f"roles: corrected {consolidate_roles(conn)} meetings by team vote")
    all_teams = [r[0] for r in conn.execute("SELECT DISTINCT team FROM meetings WHERE extracted=1" +
                                            (" AND team=?" if only_team else ""), args)]
    with ThreadPoolExecutor(workers) as pool:
        sure = sum(ok for _, ok in pool.map(roles_from_kickoff, all_teams))
    print(f"roles: {sure} of {len(all_teams)} teams confirmed from their kick-off meeting")
    conn.close()
