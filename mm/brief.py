"""Pre-meeting brief. Code picks what goes in (by status), the model only writes it up.

  [code]  pull the team's memory records from every meeting BEFORE the upcoming one,
          grouped by status: current decisions, superseded decisions (= what changed),
          open actions, open disagreements
  [LLM]   write the four sections, each point with a [meeting: "quote"] citation
  [code]  check every citation word for word; failed ones go back to the model once
"""
import re

from . import llm, store, verify

CITE = re.compile(r'\[([A-Z]{2}\d{4}[a-d]): "(.+?)"\]')

PROMPT = """Write the brief Team {team} should read before meeting {next_ord} of 4 (their final meeting).
They are designing a TV remote control. Use ONLY the memory records below (from meetings 1 to {last_ord}).

Speaker roles per meeting:
{roles}

Sections, in this order, as short bullet points:
## Decided so far          (CURRENT decisions only: the present state of the design)
## What changed along the way   (SUPERSEDED decisions: what it was, what it became, in which meeting)
## Open actions and owners      (OPEN actions: owner as "Speaker X (role)")
## Disagreements still open     (OPEN disagreements: who wants what)

Rules:
- Every bullet ends with one or two citations in exactly this form: [{team}b: "exact quote"]
  Use the record's quote exactly as given, character for character. Do not shorten or fix it.
- Merge duplicate records into one bullet. Skip small talk. Aim for 12 to 22 bullets in total.
- If a section has nothing, write "- None recorded."
- Plain text with the ## headings above. Return JSON: {{"brief": "..."}}
{feedback}
RECORDS
{records}"""


def _fmt(rows, by_id):
    out = []
    for r in rows:
        line = (f"{r['meeting_id']} {r['t']} | {r['kind']} | {r['status'].upper()} | {r['topic']}: {r['statement']} "
                f"| speaker {r['speaker']}" + (f" | owner {r['owner']}" if r["owner"] else "") +
                f" | quote: \"{r['quote']}\"")
        if r["changed_by"] and r["changed_by"] in by_id:
            n = by_id[r["changed_by"]]
            line += (f" || REPLACED in {n['meeting_id']} by: {n['statement']} | quote: \"{n['quote']}\"")
        out.append(line)
    return "\n".join(out)


def write_brief(team: str, next_ord: int = 4) -> dict:
    conn = store.connect()
    rows = store.team_records(conn, team, max_ord=next_ord - 1)
    if not rows:
        raise SystemExit(f"no memory records for {team}: run ingest first")
    by_id = {r["id"]: r for r in rows}
    # a status change made in the upcoming meeting must not leak into a brief written before it
    visible = []
    for r in rows:
        d = dict(r)
        if d["changed_by"] and d["changed_by"] not in by_id:
            d.update(status={"decision": "current", "action": "open", "disagreement": "open"}[d["kind"]],
                     changed_by=None, changed_in=None)
        visible.append(d)
    roles = "\n".join(
        f"  {m['id']}: " + ", ".join(f"{s}={r}" for s, r in sorted(store.roles_for(conn, m['id']).items()))
        for m in conn.execute("SELECT id FROM meetings WHERE team=? AND ord<? ORDER BY ord", (team, next_ord)))
    feedback, brief, errors = "", "", []
    for attempt in range(2):
        data = llm.call(PROMPT.format(team=team, next_ord=next_ord, last_ord=next_ord - 1, roles=roles,
                                      feedback=feedback, records=_fmt(visible, by_id)),
                        model=llm.SMART, stage="brief")
        brief = data["brief"] if isinstance(data, dict) else str(data)
        cites = [{"meeting_id": m, "quote": q} for m, q in CITE.findall(brief)]
        good, errors = verify.check_citations(conn, cites)
        if not errors and good:
            break
        feedback = ("\nYour previous draft had citations that are NOT word for word in the transcript. "
                    "Fix or remove these:\n" + "\n".join(errors) + "\n")
    conn.close()
    return {"brief": brief, "citations": len(CITE.findall(brief)), "unverified": errors}
