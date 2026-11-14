#!/usr/bin/env python3
"""Meeting memory agent: command line.

  python run.py index                 parse + keyword index + embeddings (code only, no LLM)
  python run.py ingest [--team X]     index, then LLM extraction + reconcile (resumable)
  python run.py stats                 what is in the store, LLM cost so far, 1,000-meeting estimate
  python run.py ask "question"        one question, prints the agent's steps
  python run.py timeline TEAM [topic] show the memory records and what is current
  python run.py answer-all            answer data/questions.json + brief -> answers.json
  python run.py brief [TEAM]          pre-final-meeting brief (default TS3012)
  python run.py eval                  score the agent on eval/my_questions.json
  python run.py check                 final gate: every citation in answers.json is word for word
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mm import agent, brief, ingest, llm, store, verify

ROOT = Path(__file__).resolve().parent
ANSWERS = ROOT / "answers.json"
TRACES = ROOT / "traces"


def cmd_stats(_):
    conn = store.connect()
    one = lambda sql: conn.execute(sql).fetchone()[0]
    n = one("SELECT COUNT(*) FROM meetings")
    print(f"meetings {n} | teams {one('SELECT COUNT(DISTINCT team) FROM meetings')} | "
          f"turns {one('SELECT COUNT(*) FROM turns')} | words {one('SELECT SUM(n_words) FROM meetings')}")
    print(f"chunks {one('SELECT COUNT(*) FROM chunks')} | vectors {one('SELECT COUNT(*) FROM vectors')} | "
          f"extracted {one('SELECT COUNT(*) FROM meetings WHERE extracted=1')} | "
          f"reconciled {one('SELECT COUNT(*) FROM meetings WHERE reconciled=1')}")
    for r in conn.execute("SELECT kind, status, COUNT(*) c FROM records GROUP BY 1,2 ORDER BY 1,2"):
        print(f"  records {r['kind']:<13}{r['status']:<11}{r['c']}")
    print("LLM calls (ok only):")
    ingest_cost = ingest_secs = 0.0
    for r in conn.execute("SELECT stage, model, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o, "
                          "SUM(cost_usd) c, SUM(seconds) s FROM llm_log WHERE ok=1 GROUP BY 1,2 ORDER BY 1"):
        print(f"  {r['stage']:<10}{r['model']:<8}calls {r['n']:<5}in {r['i']:<9}out {r['o']:<8}"
              f"${r['c']:.2f}  {r['s']:.0f}s of model time")
        if r["stage"] in ("extract", "reconcile"):
            ingest_cost += r["c"]; ingest_secs += r["s"]
    done = one("SELECT COUNT(*) FROM meetings WHERE extracted=1")
    if done:
        print(f"ingest: ${ingest_cost / done:.4f} and {ingest_secs / done:.0f}s of model time per meeting "
              f"-> 1,000 meetings = ${ingest_cost / done * 1000:.0f}, "
              f"{ingest_secs / done * 1000 / 3600:.1f} model-hours (divide by parallel workers)")


def cmd_ask(a):
    res = agent.ask(a.question, verbose=True)
    print("\nANSWER:", res["answer"])
    for c in res["citations"]:
        print(f'  [{c["meeting_id"]}] "{c["quote"]}"')
    if res["trace"]["quote_errors"]:
        print("  quote check rejected:", *res["trace"]["quote_errors"], sep="\n    ")


def cmd_timeline(a):
    conn = store.connect()
    print(agent.timeline(conn, a.team, a.topic or "", limit=200 if not a.topic else 30))


def _answer_one(q):
    try:
        res = agent.ask(q["question"], team=q.get("team"))
    except Exception as e:                     # one failure must not lose the other 24
        res = {"answer": f"ERROR: {e}", "citations": [], "trace": {"error": str(e)}}
    TRACES.mkdir(exist_ok=True)
    (TRACES / f"{q['question_id']}.json").write_text(json.dumps(res["trace"], indent=1))
    print(f"  {q['question_id']}: {len(res['citations'])} citations, {len(res['trace'].get('steps', []))} steps",
          flush=True)
    return {"question_id": q["question_id"], "answer": res["answer"], "citations": res["citations"]}


def cmd_answer_all(a):
    qs = json.loads((ROOT / "data" / "questions.json").read_text())
    qs = qs["questions"] if isinstance(qs, dict) else qs
    old = json.loads(ANSWERS.read_text()) if ANSWERS.exists() else {"answers": [], "brief": ""}
    have = {x["question_id"]: x for x in old["answers"]
            if x["citations"] and not x["answer"].startswith("ERROR")}
    todo = [q for q in qs if a.redo or q["question_id"] not in have]
    print(f"answering {len(todo)} questions ({len(have)} already done; --redo to force)")
    with ThreadPoolExecutor(a.workers) as pool:
        for ans in pool.map(_answer_one, todo):
            have[ans["question_id"]] = ans
    out = {"answers": [have[q["question_id"]] for q in qs if q["question_id"] in have],
           "brief": old.get("brief", "")}
    if a.redo or not out["brief"]:
        b = brief.write_brief("TS3012")
        out["brief"] = b["brief"]
        print(f"brief: {b['citations']} citations, {len(b['unverified'])} failed the quote check")
    ANSWERS.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"wrote {ANSWERS}")
    cmd_check(a)


def cmd_brief(a):
    b = brief.write_brief(a.team)
    print(b["brief"])
    print(f"\n[{b['citations']} citations, {len(b['unverified'])} failed the quote check]", *b["unverified"], sep="\n")


def cmd_check(_):
    """Final gate, code only: shape of answers.json and every quote word for word."""
    conn = store.connect()
    data = json.loads(ANSWERS.read_text())
    bad = 0
    for x in data["answers"]:
        good, errors = verify.check_citations(conn, x["citations"])
        if errors or not 1 <= len(x["citations"]) <= 3 or not x["answer"].strip():
            bad += 1
            print(f"  FAIL {x['question_id']}: {len(x['citations'])} citations", *errors, sep="\n    ")
    cites = [{"meeting_id": m, "quote": q} for m, q in brief.CITE.findall(data.get("brief", ""))]
    good, errors = verify.check_citations(conn, cites)
    print(f"answers: {len(data['answers'])} total, {bad} failing | brief: {len(good)} citations ok, {len(errors)} bad")
    for e in errors:
        print("  brief FAIL", e)
    return bad + len(errors)


JUDGE = """Grade an answer about meeting transcripts against a reference answer written by a human who read
the transcripts. Grade only facts, not wording.
"correct": every part of the question is answered and agrees with the reference.
"partial": some parts right, some missing or wrong.
"wrong": the main fact is wrong or missing.
Return JSON: {{"grade": "correct"|"partial"|"wrong", "why": "one sentence"}}

QUESTION: {q}
REFERENCE: {ref}
ANSWER: {ans}"""


def _eval_one(q):
    res = agent.ask(q["question"])
    g = llm.call(JUDGE.format(q=q["question"], ref=q["reference"], ans=res["answer"]),
                 model=llm.SMART, stage="judge")
    print(f"  {q['id']}: {g.get('grade')}", flush=True)
    return {"id": q["id"], "question": q["question"], "reference": q["reference"], "answer": res["answer"],
            "citations": res["citations"], "quote_errors": res["trace"]["quote_errors"],
            "steps": len(res["trace"]["steps"]), "grade": g.get("grade"), "why": g.get("why")}


def cmd_eval(a):
    qs = json.loads((ROOT / "eval" / "my_questions.json").read_text())
    with ThreadPoolExecutor(a.workers) as pool:
        rows = list(pool.map(_eval_one, qs))
    (ROOT / "eval" / "results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    for g in ("correct", "partial", "wrong"):
        print(f"{g}: {sum(r['grade'] == g for r in rows)}/{len(rows)}")
    print(f"answers with at least one verified citation: {sum(bool(r['citations']) for r in rows)}/{len(rows)}")
    print("wrote eval/results.json (grades are a model's first pass: hand-check them)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index").set_defaults(fn=lambda a: print("new meetings:", ingest.index_all(store.connect())))
    s = sub.add_parser("ingest"); s.add_argument("--team"); s.add_argument("--workers", type=int, default=4)
    s.set_defaults(fn=lambda a: ingest.run(a.workers, a.team))
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    s = sub.add_parser("ask"); s.add_argument("question"); s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("timeline"); s.add_argument("team"); s.add_argument("topic", nargs="?")
    s.set_defaults(fn=cmd_timeline)
    s = sub.add_parser("answer-all"); s.add_argument("--redo", action="store_true")
    s.add_argument("--workers", type=int, default=3); s.set_defaults(fn=cmd_answer_all)
    s = sub.add_parser("brief"); s.add_argument("team", nargs="?", default="TS3012"); s.set_defaults(fn=cmd_brief)
    s = sub.add_parser("eval"); s.add_argument("--workers", type=int, default=3); s.set_defaults(fn=cmd_eval)
    sub.add_parser("check").set_defaults(fn=lambda a: sys.exit(1 if cmd_check(a) else 0))
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
