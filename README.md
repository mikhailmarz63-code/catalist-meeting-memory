# Meeting memory agent

Turns recorded meetings into memory you can question, with word-for-word citations, and writes a brief before a team's next meeting. Built for the Catalist Agentic AI Intern build test on 138 AMI Corpus meetings.

- `answers.json`: the 25 answers and the TS3012 brief, made by the agent
- `WRITEUP.md`: the one-page write-up (architecture, model-vs-code line, eval, failures, cost)
- `EXPLAINER.md`: the whole thing in plain English

## Run it

Needs Python 3.10+ and the [Claude Code CLI](https://claude.com/claude-code) logged in (`claude auth login`). No API key.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py ingest          # index + extract + reconcile. Resumable: rerun to continue
.venv/bin/python run.py stats           # what is stored, LLM cost so far, 1,000-meeting estimate
.venv/bin/python run.py ask "Team TS3009 planned a titanium back. Was it kept in the end?"
.venv/bin/python run.py timeline TS3012 "speech recognition"   # memory records and their status
.venv/bin/python run.py answer-all      # writes answers.json (25 answers + brief), then checks it
.venv/bin/python run.py eval            # my 12 questions -> eval/results.json
.venv/bin/python run.py check           # every citation in answers.json is word for word
```

`run.py index` alone does the code-only part (parse, chunk, keyword index, embeddings) with no model calls.

## Layout

| File | What it does | LLM? |
|---|---|---|
| `mm/parse.py` | Transcript file to speaker turns and ~220-word chunks | no |
| `mm/store.py` | SQLite store: turns, chunks, FTS5 keyword index, vectors, memory records. Hybrid search | no |
| `mm/embed.py` | Local embeddings (bge-small via fastembed, CPU) | no |
| `mm/verify.py` | Word-for-word quote check | no |
| `mm/llm.py` | The single place a model is called (`claude -p`, no tools, JSON out). Logs tokens, cost, seconds | yes |
| `mm/ingest.py` | Extract records per meeting, then reconcile status per team in meeting order | yes |
| `mm/agent.py` | Ask loop: timeline / search / read / answer, with quote check and retry | yes |
| `mm/brief.py` | Pre-meeting brief from records grouped by status | yes |
| `run.py` | Command line | |
| `eval/` | My questions with hand-checked references, results, and the two helper scripts I used to read transcripts | |
| `traces/` | Every step the agent took for each of the 25 questions | |

## Swapping the model

`mm/llm.py` has one function, `_run`. Replace its body with an Anthropic API call (or any provider) and nothing else changes.

## Data

AMI Meeting Corpus, University of Edinburgh and partners, CC BY 4.0. See `data/CREDITS.txt`.
