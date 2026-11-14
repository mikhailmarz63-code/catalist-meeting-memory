"""Chat window for the meeting memory agent. Runs on this computer only.

    .venv/bin/python app.py          then open http://127.0.0.1:8765

Standard library only. The page talks to the same agent loop as `run.py ask`:
  POST /api/ask      start a question, returns a job id
  GET  /api/job/ID   steps so far, and the checked answer when done
  GET  /api/info     meeting and team counts
  GET  /api/brief    the TS3012 brief from answers.json

Security: bound to 127.0.0.1, no login because nothing leaves the machine. Host and Origin
headers are checked so another website open in the browser cannot call these endpoints.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mm import agent, store, verify

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", "8765"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
STATIC = {"/": ("ui/index.html", "text/html; charset=utf-8"),
          "/app.js": ("ui/app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("ui/app.css", "text/css; charset=utf-8")}
MAX_BODY, MAX_MESSAGE, MAX_RUNNING = 8_000, 600, 2

JOBS, LOCK = {}, threading.Lock()
TEAMS = []

SMALL_TALK = re.compile(r"^\W*(hi|hey|hello|yo|sup|thanks|thank you|thx|ok|okay|cool|nice|help|"
                        r"what can you do|who are you|how does this work)\W*$", re.I)
HELLO = ("I remember 138 recorded meetings from 35 product teams. Ask me what a team decided, what "
         "changed between meetings, who said something, or what it cost. Naming the team helps, "
         "for example TS3012. Every answer comes with quotes that were checked word for word.")
FOLLOW_UP = re.compile(r"^\W*(and|also|so|but|what about|how about|why|who|when|which|did (they|he|she|it)|"
                       r"was (it|that)|then)\b|\b(they|them|their|that|it|this)\b", re.I)


def find_team(text: str):
    """'ts3012', 'TS 3012' or just '3012' all mean team TS3012. Returns (team, text with the clean id)."""
    m = re.search(r"\b([A-Za-z]{2})[\s-]?(\d{4})(?![0-9])", text)
    if m and (m.group(1).upper() + m.group(2)) in TEAMS:
        team = m.group(1).upper() + m.group(2)
        return team, text[:m.start()] + team + text[m.end():]
    m = re.search(r"(?<![A-Za-z0-9])(\d{4})(?![0-9])", text)
    if m:
        hits = [t for t in TEAMS if t.endswith(m.group(1))]
        if len(hits) == 1:
            return hits[0], text[:m.start()] + hits[0] + text[m.end():]
    return None, text


def describe(act: dict) -> str:
    kind = act.get("action")
    if kind == "timeline":
        return f"Checking memory cards about “{str(act.get('topic', ''))[:60]}”"
    if kind == "search":
        where = f" in {act['meeting_id']}" if act.get("meeting_id") else ""
        return f"Searching transcripts for “{str(act.get('query', ''))[:60]}”{where}"
    if kind == "read":
        return f"Reading {act.get('meeting_id')} from {act.get('from')} to {act.get('to')}"
    if kind == "answer":
        return "Writing the answer and checking every quote"
    return "Thinking"


def run_job(job_id: str, message: str, prev: dict):
    job = JOBS[job_id]

    def step(text):
        with LOCK:
            job["steps"].append(text)

    try:
        team, question = find_team(message)
        prev_q, prev_a, prev_team = (str(prev.get(k) or "")[:800] for k in ("q", "a", "team"))
        all_teams = re.search(r"\b(which|what|any|every|all|another|other) teams?\b", message, re.I)
        if not team and prev_team in TEAMS and FOLLOW_UP.search(message) and not all_teams:
            # a follow-up like "and who objected?": keep the team and hand over the last exchange
            team = prev_team
            question = (f"(Earlier in this chat, about Team {team}, the user asked: {prev_q} "
                        f"The answer given was: {prev_a}) Follow-up question: {question}")
        step(f"Team {team}: looking only at that team's meetings" if team
             else "No team named: searching all 138 meetings")
        res = agent.ask(question, team=team, on_step=lambda act: step(describe(act)))
        conn = store.connect()
        cites = []
        for c in res["citations"]:
            row = verify.find_quote(conn, c["meeting_id"], c["quote"])
            cites.append({"meeting_id": c["meeting_id"], "quote": c["quote"],
                          "speaker": row["speaker"] if row else "", "t": row["t"] if row else ""})
        conn.close()
        with LOCK:
            job["result"] = {"answer": res["answer"], "citations": cites, "team": team or "",
                             "rejected": len(res["trace"]["quote_errors"]),
                             "seconds": round(time.time() - job["t0"])}
    except Exception as e:                      # full detail to the terminal, clean message to the page
        print(f"[{job_id}] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        text = str(e)
        if "not logged in" in text.lower():
            msg = "The AI is not signed in. Run `claude auth login` in Terminal once, then ask again."
        elif "limit" in text.lower():
            msg = "The AI usage limit is reached for now. Try again after it resets."
        else:
            msg = f"Something went wrong while answering. Reference {job_id}."
        with LOCK:
            job["error"] = msg
    finally:
        with LOCK:
            job["done"] = True


class Handler(BaseHTTPRequestHandler):
    server_version = "MeetingMemory"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def _host_ok(self):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self._json(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            return self._send(200, (ROOT / name).read_bytes(), ctype)
        if path == "/api/info":
            conn = store.connect()
            n = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
            conn.close()
            return self._json(200, {"meetings": n, "teams": TEAMS})
        if path == "/api/brief":
            f = ROOT / "answers.json"
            brief = json.loads(f.read_text()).get("brief", "") if f.exists() else ""
            return self._json(200, {"team": "TS3012", "brief": brief})
        m = re.fullmatch(r"/api/job/([0-9a-f]{12})", path)
        if m and m.group(1) in JOBS:
            with LOCK:
                j = JOBS[m.group(1)]
                return self._json(200, {k: j.get(k) for k in ("steps", "done", "result", "error")})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return
        origin = self.headers.get("Origin", "")
        if origin.split("://", 1)[-1] not in ALLOWED_HOSTS or \
                not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(403, {"error": "forbidden"})
        if self.path != "/api/ask":
            return self._json(404, {"error": "not found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError
            data = json.loads(self.rfile.read(size))
            message = " ".join(str(data["message"]).split())
            prev = data.get("prev") if isinstance(data.get("prev"), dict) else {}
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "Send a question as text."})
        if not message or len(message) > MAX_MESSAGE:
            return self._json(400, {"error": f"Questions can be up to {MAX_MESSAGE} characters."})
        if SMALL_TALK.match(message):
            return self._json(200, {"reply": HELLO})
        with LOCK:
            for k in [k for k, j in JOBS.items() if j["done"] and time.time() - j["t0"] > 900]:
                del JOBS[k]
            if sum(not j["done"] for j in JOBS.values()) >= MAX_RUNNING:
                return self._json(429, {"error": "Still working on an earlier question. Give it a moment."})
            job_id = uuid.uuid4().hex[:12]
            JOBS[job_id] = {"steps": [], "done": False, "result": None, "error": None, "t0": time.time()}
        threading.Thread(target=run_job, args=(job_id, message, prev), daemon=True).start()
        self._json(200, {"job": job_id})


def main():
    if not shutil.which("claude"):             # launched by double-click: the shell profile is not loaded
        os.environ["PATH"] += os.pathsep + str(Path.home() / ".local/bin")
    conn = store.connect()
    TEAMS.extend(r[0] for r in conn.execute("SELECT DISTINCT team FROM meetings ORDER BY 1"))
    conn.close()
    url = f"http://{HOST}:{PORT}"
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Meeting memory chat is running at {url}  (Ctrl+C to stop)", flush=True)
    if "--no-open" not in sys.argv:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
