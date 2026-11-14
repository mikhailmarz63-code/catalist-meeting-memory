"""The ask loop. Python owns the loop; the model only chooses the next step.

  code : find the team id in the question, pre-fetch a first batch of evidence
  model: pick ONE action -> timeline | search | read | answer
  code : run the action, hand the result back            (max MAX_STEPS model calls)
  code : check every quote word for word; on failure tell the model exactly which quote failed

The model never sees more than a few retrieved chunks at a time, whether the store
holds 138 meetings or 10,000.
"""
import json
import re

from . import llm, store, verify

MAX_STEPS = 7
MAX_QUOTE_RETRIES = 2
TEAM = re.compile(r"\b([A-Z]{2}\d{4})(?![a-d0-9])")
_TOK = re.compile(r"[a-z0-9]+")

PROMPT = """You answer questions about recorded meetings from a memory store. You cannot see the meetings
directly; you must retrieve. Speech is raw (false starts, "um", crosstalk).

SCOPE
{scope}

TOOLS: reply with exactly ONE JSON action per turn.
{{"action": "timeline", "topic": "words"}}
   Memory records (decisions, actions, disagreements) for this team in meeting order, each with a status:
   current/superseded (decisions), open/done (actions), open/resolved (disagreements). Fast map of what changed.
   Records are machine-extracted and can be incomplete or wrong: confirm against the transcript before answering.
{{"action": "search", "query": "words", "meeting_id": "optional, e.g. ES2008b"}}
   Hybrid keyword + meaning search over transcript chunks. Use the team's own words (try synonyms: "kinetic",
   "solar", "battery"; "LCD", "screen", "display").
{{"action": "read", "meeting_id": "ES2008b", "from": "00:12:00", "to": "00:16:00"}}
   Raw turns in a time window. Use it to see who said what around a hit and to copy exact quotes.
{{"action": "answer", "answer": "...", "citations": [{{"meeting_id": "ES2008b", "quote": "exact words"}}]}}

HOW TO WORK
- Questions span more than one meeting. Find the earliest relevant moment AND the latest: a later meeting can
  overturn an earlier decision. Check the last meeting before saying what was final.
- "Who": name the speaker letter and role, e.g. "Speaker B (industrial designer)". Roles are listed per meeting
  in SCOPE; a letter is only meaningful inside its own meeting. Read the turns to see who actually said it first.
- Numbers: report figures exactly as spoken in the transcript. Never calculate a figure yourself and never
  assume a budget or price you have not seen stated for this team. Prices are spoken loosely: "twelve fifty"
  means 12.50 and "seventeen fifty Euro" means 17.50 Euro, not 1750. Read them against the 12.50 budget.
- If evidence is thin or conflicting, search again with different words or read around the hit. Do not guess.
- If the store truly does not contain the answer, say so in the answer rather than inventing one.

ANSWER FORMAT
- One or two sentences, 60 words at most, that answer every part of the question with specifics (numbers,
  materials, names). No background, no hedging, no repeating the question.
- If the question assumes something the meetings contradict, say so and give what actually happened.
- 1 to 3 citations. Each quote is copied character for character from ONE speaker turn, 5 to 40 words,
  no speaker label, no timestamp, no "..." and nothing fixed or tidied. Quotes are machine-checked word for word.
- Pick quotes that prove the key facts, ideally from different meetings.

QUESTION
{question}

WORK SO FAR
{history}

Your next JSON action:"""


def _scope(conn, team):
    if not team:
        return "No team named in the question: search covers all meetings."
    lines = [f"Team {team}. All tools are filtered to this team's meetings:"]
    for m in conn.execute("SELECT id, ord FROM meetings WHERE team=? ORDER BY ord", (team,)):
        roles = store.roles_for(conn, m["id"])
        r = ", ".join(f"{s}={roles[s]}" for s in sorted(roles)) or "roles unknown"
        lines.append(f"  {m['id']} (meeting {m['ord']}): {r}")
    return "\n".join(lines)


def _stem(s):
    return {w[:6] for w in _TOK.findall(s.lower()) if len(w) > 2}


def timeline(conn, team, topic, limit=30) -> str:
    rows = store.team_records(conn, team)
    if not rows:
        return "(no memory records for this team)"
    want = _stem(topic)
    # ponytail: word-overlap ranking over one team's records (tens to hundreds of rows).
    # Give records their own embeddings if a single team ever holds thousands.
    scored = sorted(rows, key=lambda r: -len(want & _stem(r["topic"] + " " + r["statement"])))
    keep = [r for r in scored if want & _stem(r["topic"] + " " + r["statement"])][:limit] or scored[:limit]
    keep.sort(key=lambda r: (r["ord"], r["t"], r["id"]))
    out = []
    for r in keep:
        line = (f"#{r['id']} {r['meeting_id']} {r['t']} | {r['kind']} | {r['status'].upper()} | {r['topic']}: "
                f"{r['statement']} | said by {r['speaker']}: \"{r['quote']}\"")
        if r["changed_in"]:
            line += f" | changed in {r['changed_in']} by #{r['changed_by']}: {r['note']}"
        out.append(line)
    return "\n".join(out)


def search(conn, team, query, meeting_id=None, k=6) -> str:
    hits = store.hybrid_search(conn, query, team=team, meeting_id=meeting_id, k=k)
    if not hits:
        return "(no hits)"
    hits.sort(key=lambda c: (c["meeting_id"], c["start_idx"]))
    return "\n\n".join(f"--- {c['meeting_id']} {c['t_start']}-{c['t_end']}\n{c['text']}" for c in hits)


def read(conn, team, meeting_id, t_from, t_to) -> str:
    if team and not str(meeting_id).startswith(team):
        return f"(meeting {meeting_id} is outside team {team})"
    rows = store.read_turns(conn, meeting_id, t_from, t_to)
    return "\n".join(f"[{r['t']}] {r['speaker']}: {r['text']}" for r in rows) or "(no turns in that window)"


def _clip(text, words=1800):
    w = text.split(" ")
    return text if len(w) <= words else " ".join(w[:words]) + " ...(clipped)"


def ask(question: str, team: str | None = None, verbose=False) -> dict:
    conn = store.connect()
    if not team:
        m = TEAM.search(question)
        team = m.group(1) if m else None
    if team and not conn.execute("SELECT 1 FROM meetings WHERE team=?", (team,)).fetchone():
        team = None
    scope = _scope(conn, team)
    # step 0 is free: code pre-fetches a first look so the model starts with evidence
    history = []
    if team:
        history.append(("timeline (auto)", timeline(conn, team, question)))
    history.append(("search (auto)", search(conn, team, question)))
    trace = {"question": question, "team": team, "steps": [], "quote_errors": []}
    result, retries, has_read = None, 0, False

    for step in range(MAX_STEPS):
        last = step == MAX_STEPS - 1
        hist = "\n\n".join(f"### {name}\n{_clip(obs)}" for name, obs in history)
        if last:
            hist += "\n\n### system\nThis is your last step. You must reply with the answer action now."
        act = llm.call(PROMPT.format(scope=scope, question=question, history=hist),
                       model=llm.SMART, stage="ask")
        if not isinstance(act, dict):
            history.append(("system", "Reply must be one JSON object with an 'action' key."))
            continue
        kind = act.get("action")
        trace["steps"].append(act)
        if verbose:
            print(f"  step {step + 1}: {json.dumps(act)[:200]}", flush=True)
        if kind == "timeline" and team:
            history.append((f"timeline {act.get('topic', '')!r}", timeline(conn, team, str(act.get("topic", "")))))
        elif kind == "search":
            history.append((f"search {act.get('query', '')!r} {act.get('meeting_id') or ''}",
                            search(conn, team, str(act.get("query", "")), act.get("meeting_id") or None)))
        elif kind == "read":
            has_read = True
            history.append((f"read {act.get('meeting_id')} {act.get('from')}-{act.get('to')}",
                            read(conn, team, str(act.get("meeting_id", "")), str(act.get("from", "00:00:00")),
                                 str(act.get("to", "99:99:99")))))
        elif kind == "answer" and not has_read and not last:
            # Code rule, added after the first eval run: answers written straight from memory records got
            # numbers and "who said it" wrong. So no answer is accepted until raw turns have been read.
            has_read = True
            history.append(("system: answer not accepted yet",
                            "You have not read any raw transcript turns. Use read on the moments your answer "
                            "depends on (who spoke first, the exact figures), then answer."))
        elif kind == "answer":
            good, errors = verify.check_citations(conn, act.get("citations"))
            result = {"answer": str(act.get("answer", "")).strip(), "citations": good[:3]}
            if not errors and good:
                break
            trace["quote_errors"].extend(errors)
            if retries >= MAX_QUOTE_RETRIES or last:
                break                      # keep only the quotes that passed
            retries += 1
            history.append(("system: citation check FAILED", "\n".join(errors) + (
                "\nYou gave no valid citation." if not good else "") +
                "\nCopy quotes character for character from a single turn shown above (use read to see the "
                "exact turn), then answer again."))
        else:
            history.append(("system", f"Unknown action {kind!r}. Use timeline, search, read or answer."))
    conn.close()
    if result is None:
        result = {"answer": "No answer found in the meeting store.", "citations": []}
    result["trace"] = trace
    return result
