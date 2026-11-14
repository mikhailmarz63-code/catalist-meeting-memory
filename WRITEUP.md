# Write-up: meeting memory agent

## Architecture

```
INGEST, once per meeting                         ASK, per question
 [code] parse turns, cut ~220-word chunks         [code] find team id, filter to that team
 [code] keyword index (SQLite FTS5)               [code] pre-fetch memory records + top chunks
 [code] local embedding per chunk                 [LLM ] choose a step: timeline | search | read | answer
 [LLM ] one meeting -> decisions, actions,        [code] run the step, hand back the result (max 7 steps)
        disagreements, each with a quote          [code] no answer accepted before raw turns were read
 [code] drop records whose quote is not           [code] check every quote word for word; on failure
        word for word in the transcript                  name the bad quote and make the model retry
 [LLM ] per team, in meeting order: does a
        new record replace or close an old one?
 [code] write status: current / superseded,
        open / done, open / resolved
```

One SQLite file holds everything. The store is the system of record; the model only ever sees one meeting (ingest) or a handful of retrieved chunks (ask). Nothing depends on the corpus fitting in a context window.

**How it knows what is current.** Every record carries a status and, when replaced, a pointer to the record and meeting that replaced it. Reconcile shows the model only that team's still-live earlier records, so the prompt stays small however long a team has been meeting. Across the 138 meetings it marked 190 decisions superseded, 35 actions done and 32 disagreements resolved. The brief is a query over those statuses, limited to meetings before the one being briefed.

## Model versus code

| Plain code, never a model | Why |
|---|---|
| Parsing, chunking, meeting order, team filter | Fixed format, right answer exists, free |
| Search and ranking | Must stay sub-second at 10,000 meetings |
| Quote verification | "Checked word for word" is a string match. A model's opinion that a quote is right is worth nothing |
| Status bookkeeping, role vote, JSON assembly | Rules. Code also rejects model output that breaks them (a "supersedes" link pointing at an action is ignored) |

| Model | Why |
|---|---|
| Extracting decisions, actions, disagreements | "Um, yeah, okay let's go with that then" is a decision. Only reading finds it |
| Deciding that c replaced a | Same subject in different words: "docking station" and "the recharger thing" |
| Choosing searches, writing the answer and brief | Multi-step reasoning over evidence |

The model is never trusted to check itself. Of 2,782 records it proposed, code threw out 539 (about 1 in 5) because the quote was not verbatim: it had tidied an "um" or joined two turns. Those would have failed your citation check.

Models: Haiku for extraction and reconcile (high volume, simple reading), Sonnet for answering and the brief (reasoning). Embeddings are local (bge-small on CPU), so indexing costs nothing and no text leaves the machine for that step.

## Proof it works

12 questions of my own on teams not in your 25, references checked against the transcripts (`eval/my_questions.json`, each with meeting and timestamp). They include a false-premise trap and one that names no team.

| | Correct | Partial | Wrong | Citations verified |
|---|---|---|---|---|
| Model judge | 7 | 5 | 0 | 12 of 12 |
| After I hand-checked each grade | 9 | 3 | 0 | 12 of 12 |

Two judge calls were overturned by hand. On E06 the agent said the cost fell to 11.60; my reference had missed that figure and the transcript confirms the agent. On E01 the judge marked it down for naming one objector instead of two.

**Cost and time to ingest (measured).** 138 meetings: 326 model calls, 1.95M tokens in, 1.26M out, 9.86 dollars at API prices, about 30 minutes wall time with 8 parallel workers. That is about 7 cents and 88 seconds of model time per meeting. **1,000 meetings: about 71 dollars and roughly 3 hours at 8 workers.** Answering costs about 10 cents and 25 seconds per question.

## Three things it got wrong

1. **Misread a spoken price (your Q17).** "Seventeen fifty Euro" became "€1750 per remote". The quote was verbatim, so the quote check passed. I caught it reading the answers. Fixed with a prompt rule about loosely spoken prices. Lesson: a correct citation does not make the sentence above it correct.
2. **Wrong person (my E07).** Two speakers said "plastic instead of rubber" in the same second. The agent credited C, the transcript lists B first. In the first run it also invented a "25 Euro budget" that nobody stated.
3. **Right story, missing the numbers (my E02, E09).** It said speech recognition was cut "for cost" but not "17 Euros against 12.50, and 90 percent wanted the LCD against 60". Cause: it answered from the memory record's summary instead of the raw turns. I added a code rule that rejects any answer given before a `read`. It helped one question (E05) and the model often just answered again, so the rule is too weak.

Also wrong, and worth saying: **speaker roles.** The model swapped the two designers in most meetings. My first fix, a majority vote in code, made TS3012 confidently wrong, and I only saw it because the brief contradicted the kickoff. Now one small call per team reads the kickoff introductions; 27 of 35 teams were confirmed that way and 8 still rely on the vote.

## Tools, and where they broke

- **Claude Code CLI as the model backend (`claude -p`).** Chosen because it needs no API key. It broke three ways: it silently injected my personal instruction file into every call until I isolated settings; the subscription cap hit mid-ingest and all 35 reconcile jobs failed (resumable ingest meant nothing was lost); and each call pays process start-up, which is fine at 138 meetings and wrong at 10,000.
- **SQLite FTS5 + brute-force vectors.** Fine here (3,776 chunks, always filtered to one team). Unfiltered search at 10,000 meetings is about 275,000 chunks and needs a real vector index.
- **No framework.** The loop is 60 lines of Python. A framework would have hidden exactly the parts you are assessing.

## What production needs

- Direct API with batch pricing for ingest (about half the cost) and prompt caching for the ask loop.
- Postgres with pgvector, or sqlite-vec, instead of brute-force cosine.
- Embeddings on memory records, so "timeline" works for a team with thousands of records.
- Real speaker identity from the recorder, instead of inferring roles.
- Reconcile across a whole project, not only against the previous meetings' live records, plus a human "this is wrong" button that writes a correcting record.
- A standing eval set that runs on every prompt change. 12 questions is a smoke test, not a benchmark.
