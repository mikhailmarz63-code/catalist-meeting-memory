# Explainer: what this is and how it works, in plain English

Written so you can talk through it in the demo without reading code.

## What Catalist asked for

"A founder records every meeting. Months later nobody can find what was decided. Build something that remembers."

Three jobs:

1. **Ingest.** Read every meeting once and file it away.
2. **Answer.** 25 questions, each needing more than one meeting, each with proof: the meeting id and the exact words.
3. **Brief.** A cheat sheet for Team TS3012 before their last meeting.

**The trap.** You could paste all 138 transcripts into an AI and ask. That works today and fails at 10,000 meetings, because an AI can only read so much at once (its "context window", think of it as the size of its desk). They said plainly that this is the wrong answer. So the real test is: how do you find the right few pages first?

## The words you need

| Word | Meaning |
|---|---|
| **LLM** | The AI model. Here, Claude. Good at reading messy language, slow and costly compared to code, and it can make things up |
| **Ingest** | Reading a meeting once and storing it in a searchable form |
| **Chunk** | A small slice of a transcript, about 220 words. We search chunks, not whole meetings |
| **Keyword search** | Finds chunks that contain the same words as the question. Fast and exact |
| **Embedding** | A way to turn text into a list of numbers so that texts with similar meaning get similar numbers. Lets us find "battery" when the question said "power source" |
| **Hybrid search** | Keyword search and embedding search together. A chunk either one ranks high gets through |
| **Retrieval** | Searching the store and handing the AI only what it needs |
| **Agent** | An AI in a loop. It picks a step (search, read, answer), sees the result, picks the next step |
| **Citation** | The proof. Meeting id plus exact words. The graders compare letter for letter |
| **Supersede** | A later decision replacing an earlier one |

## How it works, as a story

Think of a law office with a filing clerk and a senior lawyer.

**The filing clerk is plain code.** Fast, free, never invents anything.
- Splits each transcript into speaker turns and chunks.
- Builds two indexes: one by word, one by meaning.
- Knows meeting a comes before b, c, d.
- Checks every quote against the original, letter for letter.

**The lawyer is the AI.** Expensive, clever, sometimes wrong. Used only where reading comprehension is needed.
- Reads one meeting at a time and writes index cards: "DECISION: case will be rubber", "ACTION: B will check the cost of the display", "DISAGREEMENT: C wants a scroll wheel, A does not". Each card carries the exact quote that proves it.
- Compares new cards with the team's older cards and marks what got replaced: "LCD dropped (meeting b)" becomes SUPERSEDED when meeting c says "LCD is back in".
- Answers questions by searching, reading, and searching again until it has the facts.

**The clerk checks the lawyer.** Every quote the AI produces is checked by code against the transcript. If it is not there word for word, the card is thrown out, or the AI is told which quote failed and must try again. The AI is never trusted to mark its own homework.

## Answering one question, step by step

Question: "Team TS3009 planned a titanium back. Who first proposed replacing it, with what, and was titanium kept?"

1. **Code** spots "TS3009" and limits everything to that team's four meetings.
2. **Code** pulls the team's index cards about titanium, in time order, plus the best matching chunks.
3. **AI** looks and decides: "I need to see who spoke first. Search meeting c for titanium."
4. **Code** runs the search and returns the chunks.
5. **AI**: "Now read meeting d around minute 20 to see if it was kept."
6. **Code** returns those turns.
7. **AI** writes the answer and picks quotes.
8. **Code** checks each quote letter for letter. Fail means "try again". Pass means it goes into `answers.json`.

The AI never sees more than a few pages at once. That is why the same design works for 138 meetings or 10,000.

## How it knows what is current

Every index card has a status.

| Card type | Statuses |
|---|---|
| Decision | current, or superseded (with a pointer to the card that replaced it) |
| Action | open, or done |
| Disagreement | open, or resolved |

When meeting c is filed, the AI is shown only that team's still-live cards from earlier meetings and asked "does anything here replace or close one of these?". Code then updates the status. The brief is built from this: current decisions, superseded ones (what changed), open actions, open disagreements.

## What you should be ready to say out loud

- **Why not paste everything in?** It does not scale and it costs more per question. Retrieval keeps each AI call small no matter how big the store gets.
- **What never touches the AI?** Parsing, indexing, search, ordering, and quote checking. They have right answers that code gets every time, for free.
- **Why does the AI do extraction?** Because "um, yeah, okay let's go with that then" is a decision, and only something that reads language can tell.
- **Where is it weak?** See the failures section of WRITEUP.md. Say them plainly, they score for honesty.
