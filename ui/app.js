// Chat page for the meeting memory agent. Text is always set with textContent, never as HTML.
const $ = (id) => document.getElementById(id);
const thread = $("thread"), input = $("input"), send = $("send"), scroller = $("scroll"), scope = $("scope");

const EXAMPLES = [
  ["Changed its mind", "did ts3012 keep speech recognition in the end?"],
  ["Over budget", "Team ES2016 went over budget. What did they remove to fix it?"],
  ["Who said it", "who in IS1006 first suggested titanium, and why?"],
  ["No team named", "Which team's final costing was nine pounds twenty?"],
];

let prev = null;      // last question, answer and team, so "and who objected?" keeps its context
let busy = false;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function toBottom() { scroller.scrollTop = scroller.scrollHeight; }
function setBusy(b) { busy = b; send.disabled = b || !input.value.trim(); }
function grow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; }

function showScope() {
  scope.textContent = "";
  scope.hidden = !(prev && prev.team);
  if (scope.hidden) return;
  scope.append(`Follow-up questions stay on Team ${prev.team}.`);
  const b = el("button", "", "Change topic");
  b.type = "button";
  b.onclick = () => { prev = null; showScope(); input.focus(); };
  scope.append(b);
}

function addMe(text) {
  $("empty")?.remove();
  const m = el("div", "msg me");
  m.append(el("div", "bubble", text));
  thread.append(m);
}
function addCard() {
  const m = el("div", "msg"), card = el("div", "card");
  m.append(card);
  thread.append(m);
  return card;
}
function stepList(steps) {
  const ul = el("ul", "steps");
  steps.forEach((s) => ul.append(el("li", "", s)));
  return ul;
}

function renderAnswer(card, r, steps) {
  card.className = "card";
  card.textContent = "";
  card.append(el("p", "answer", r.answer));
  if (r.citations.length) {
    card.append(el("div", "label", "Proof from the meetings"));
    for (const c of r.citations) {
      const box = el("div", "cite");
      box.append(el("q", "", c.quote));
      const meta = el("div", "meta");
      meta.append(el("span", "pill", c.meeting_id));
      if (c.speaker) meta.append(el("span", "", `Speaker ${c.speaker}${c.t ? " at " + c.t : ""}`));
      meta.append(el("span", "checked", "✓ found word for word"));
      box.append(meta);
      card.append(box);
    }
  } else {
    card.append(el("p", "foot note", "No quote passed the word for word check, so treat this answer as unproven."));
  }
  const d = el("details"), s = el("summary", "", `How I found this: ${steps.length} steps in ${r.seconds} seconds`);
  d.append(s, stepList(steps));
  if (r.rejected) d.append(el("p", "note", `${r.rejected} quote${r.rejected > 1 ? "s were" : " was"} rejected by the check and rewritten.`));
  card.append(d);
}

async function poll(card, jobId) {
  const t0 = Date.now();
  card.classList.add("working");
  const ul = stepList(["Starting"]), foot = el("div", "foot"), timer = el("span", "timer", "0s");
  foot.append(timer);
  card.append(ul, foot);
  let shown = 0;
  for (;;) {
    await new Promise((r) => setTimeout(r, 800));
    timer.textContent = Math.round((Date.now() - t0) / 1000) + "s";
    let j;
    try {
      const res = await fetch("/api/job/" + jobId);
      if (!res.ok) throw new Error();
      j = await res.json();
    } catch { continue; }              // one missed poll is fine, try again
    if (j.steps.length > shown) {
      if (shown === 0) ul.textContent = "";
      j.steps.slice(shown).forEach((s) => ul.append(el("li", "", s)));
      shown = j.steps.length;
      toBottom();
    }
    if (j.done) return j;
  }
}

async function ask(text) {
  if (busy || !text.trim()) return;
  setBusy(true);
  addMe(text.trim());
  const card = addCard();
  toBottom();
  try {
    const res = await fetch("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text.trim(), prev }),
    });
    const data = await res.json();
    if (data.reply) card.append(el("p", "plain", data.reply));
    else if (data.error) card.append(el("p", "error", data.error));
    else {
      const j = await poll(card, data.job);
      if (j.error) { card.className = "card"; card.textContent = ""; card.append(el("p", "error", j.error)); }
      else {
        renderAnswer(card, j.result, j.steps);
        prev = { q: text.trim(), a: j.result.answer, team: j.result.team };
        showScope();
      }
    }
  } catch {
    card.textContent = "";
    card.append(el("p", "error", "Could not reach the app. Is it still running in Terminal?"));
  }
  setBusy(false);
  toBottom();
  input.focus();
}

// The brief is stored as lines: "## Heading" and "- point [TS3012a: "quote"] ..."
function renderBrief(card, brief) {
  const box = el("div", "brief");
  let ul = null;
  for (const raw of brief.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("#")) { box.append(el("h3", "", line.replace(/^#+\s*/, ""))); ul = null; continue; }
    if (!ul) { ul = el("ul"); box.append(ul); }
    const li = el("li"), cite = /\[([A-Z]{2}\d{4}[a-d]): "(.+?)"\]/g;
    li.append(line.replace(/^[-*]\s*/, "").replace(cite, "").replace(/\s+/g, " ").trim());
    for (const m of line.matchAll(cite)) {
      const ref = el("span", "ref");
      ref.append(el("b", "", m[1]), ` “${m[2]}”`);
      li.append(ref);
    }
    ul.append(li);
  }
  card.append(box);
}

$("briefBtn").onclick = async () => {
  if (busy) return;
  addMe("Show me the brief for Team TS3012 before their final meeting");
  const card = addCard();
  try {
    const data = await (await fetch("/api/brief")).json();
    if (data.brief) { renderBrief(card, data.brief); prev = { q: "TS3012 brief", a: "", team: "TS3012" }; showScope(); }
    else card.append(el("p", "error", "No brief found. Run `run.py brief` first."));
  } catch { card.append(el("p", "error", "Could not load the brief.")); }
  card.parentElement.scrollIntoView({ block: "start" });
};
$("newBtn").onclick = () => location.reload();

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value;
  input.value = ""; grow();
  ask(text);
});
input.addEventListener("input", () => { grow(); send.disabled = busy || !input.value.trim(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").requestSubmit(); }
});

for (const [kind, q] of EXAMPLES) {
  const b = el("button", "chip");
  b.type = "button";
  b.append(el("small", "", kind), q);
  b.onclick = () => ask(q);
  $("chips").append(b);
}
fetch("/api/info").then((r) => r.json()).then((d) => {
  $("counts").textContent = `${d.meetings} meetings from ${d.teams.length} teams, on this computer`;
}).catch(() => { $("counts").textContent = "Store not reachable"; });
setBusy(false);
input.focus();
