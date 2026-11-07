# I Stopped Treating Agent Memory Like Prompt History

A model can tell me that a startup resembles a company I evaluated two
years ago. It can even invent a plausible loss, founder problem, and
market lesson to make the comparison sound convincing. The difficult
part is not getting an answer. It is knowing whether the history behind
that answer actually happened.

That became the design problem behind my project. I built a Python
application that uses Hindsight to give two workflows access to the same
accumulated experience: business-pitch evaluation and technical code
review. The interesting part is not that both workflows can recall old
records. It is that I had to make the application distinguish historical
evidence from generated interpretation.

I came away with a fairly strong opinion: agent memory needs provenance
and boundaries just as much as it needs retrieval.

## Two audits, one accumulated history

The application has a Streamlit dashboard with two audit modes.

In Business Pitch mode, I provide a startup pitch. The audit evaluates
the team, market, and product, identifies red flags, and produces a
verdict. In Technical Code mode, I provide code and receive a security
assessment, identified bugs, and a corrected version.

Both workflows begin with a Hindsight recall against the relevant
historical material. A business audit looks for prior investment cases;
a code audit looks for previously encountered bugs and fixes. The
retrieved records are then included as context for a Groq model call,
which returns a structured JSON response.

The second part of the project is a runtime supervisor, `autoheal.py`.
It watches a service, captures crashes, and can use previously resolved
bugs as precedent when deciding whether a repair is appropriate. That
gives the memory bank a second source of engineering experience: not
just what an audit identified, but what happened when a service failed
and a repair was tested.

The common store is what makes the architecture interesting. A record is
not just a paragraph copied into a prompt for one request. It can become
relevant to a later decision in a different workflow. A past SQL
injection finding can inform a new code review. A resolved empty-input
bug can help explain a later crash. A previous investment outcome can
give a new pitch evaluation a concrete historical comparison.

I use [Hindsight](https://github.com/vectorize-io/hindsight) for
retaining and retrieving that experience. The application remains
responsible for interpreting the result correctly.

## The problem with asking a model what happened before

The first issue I wanted to address was easy to underestimate: a
language model is perfectly capable of inventing a past case when the
memory lookup finds nothing.

Imagine evaluating a pitch for a peer-to-peer payments company. The
pitch shows fast user growth, no clear revenue model, and a business
that depends heavily on one founder. A model can produce reasonable
concerns from those facts alone. It can also generate a
convincing-sounding comparison to a previous payments startup, complete
with an outcome that was never in the memory bank.

That is not a minor factual slip. It changes the basis of the decision.
The reader may believe the system is drawing on an actual investment
outcome when it is only generating a plausible story.

I decided that the model should not be the authority on whether a
historical match exists. The application checks the recall result and
overrides the relevant fields when no records were returned:

``` python
if not memories:
    audit["similar_idea_found"] = False
    audit["historical_loss"] = "None found"
    audit["similar_past_cases"] = []
```

This is a small piece of code with an important responsibility. The
model can still assess the pitch on its fundamentals. It can discuss
market risk, product gaps, or team concerns. But it cannot turn an empty
retrieval result into a historical precedent.

I prefer enforcing that distinction in code rather than relying on a
system prompt that says not to make things up. Prompts influence model
behavior; application logic can define what the application is willing
to treat as evidence.

That separation is especially important when the output contains numbers
or outcomes. A generated estimate and a recorded historical loss may
both look like ordinary text in a response, but they have very different
evidentiary status.

## A record's type is part of its meaning

The empty-recall case was only half the problem. The other half appeared
after the application began writing its own audit results back into
memory.

Suppose I evaluate a startup pitch but do not invest. The audit records
concerns about monetization and founder concentration. Later, another
pitch triggers a recall of that evaluation. If the record is not clearly
labelled, the model might describe it as an actual investment that
failed.

The content may be accurate as an evaluation, but the interpretation is
wrong. An evaluated pitch is not an investment. A risk identified by an
audit is not a realized loss. A proposed code fix is not necessarily a
verified repair.

I addressed this by labelling saved audits explicitly as an evaluated
pitch, not an actual investment. The instruction accompanying those
records also makes clear that they must not be represented as profit or
loss.

This is a data-modeling problem disguised as a prompt problem. If the
memory store contains different kinds of facts, those distinctions need
to survive retrieval. Otherwise the model receives a collection of text
fragments and has to infer whether each one describes an observation, a
prediction, an evaluation, or an outcome.

That is too much ambiguity to leave implicit.

The same principle applies to code history. A finding that says "this
query may be vulnerable" is different from a record of a confirmed SQL
injection bug and its resolved fix. A proposed patch should not be
stored as if it had already been validated. The runtime supervisor
handles that distinction by retaining a fix only after the service has
remained stable for a configured interval.

The broader lesson is that memory quality is not only about whether the
right text comes back. It is also about whether the retrieved text still
carries the meaning it had when it was written.

## Recall output needs a preparation layer

The first retrieval experiment exposed a different integration issue. I
seeded the memory bank with 14 records: seven past investments and seven
resolved bugs. The first recall returned 99 results.

Hindsight breaks retained records into smaller facts, which gives
retrieval more ways to find relevant details. But those facts can
overlap. Passing all 99 results directly into a model prompt created a
large amount of repetition and exceeded the request limits of the model
endpoint.

The useful response was not to make the prompt larger. It was to prepare
the recalled material before sending it downstream.

I added a small function that normalizes whitespace, removes exact
duplicates case-insensitively, truncates long entries, and caps the
number of memories:

``` python
def prepare_memories(texts, limit=12, chars=700):
    seen, out = set(), []
    for t in texts:
        t = " ".join(str(t).split())
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t[:chars])
        if len(out) >= limit:
            break
    return out
```

This is not intended to be a universal retrieval-ranking algorithm. It
is a practical boundary between the memory system and the model request.
It makes the context size more predictable and avoids spending tokens
repeating the same fact in slightly different forms.

It also clarified an architectural point for me: retention, retrieval,
and prompt construction are separate operations.

Retention determines what experience enters the bank. Retrieval finds
facts that may be relevant to a new task. Prompt construction decides
which of those facts are useful and how much of them the model can
reasonably consume. Treating those as one operation makes it harder to
understand where poor results originate.

The [Hindsight documentation](https://hindsight.vectorize.io/) is useful
background for understanding the memory layer itself. In the
application, though, I still need to decide how retrieved results fit
the constraints of a particular model call.

## The integration detail that Streamlit made visible

There was also a less conceptual problem: asynchronous execution.

The Hindsight client performs asynchronous work internally. Streamlit
reruns its script as users interact with the application, which makes
careless reuse of event-loop-bound objects troublesome. Caching the
client led to an "Event loop is closed" failure on a later rerun.
Creating it directly in the script's execution context could instead
produce an "event loop is already running" error.

The approach that worked was to run each Hindsight operation in a
short-lived worker thread with its own event loop:

``` python
def run_hindsight(fn):
    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return fn(get_hindsight_client())
        finally:
            loop.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(worker).result()
```

The wrapper contains the event-loop lifecycle rather than spreading
assumptions about it across the audit code. The caller can request a
Hindsight operation without needing to know whether Streamlit currently
has an active loop in its script thread.

This is an implementation choice for this application's execution model,
not a claim that every asynchronous client needs a dedicated thread. The
important thing was making ownership of the loop explicit and ensuring
it is closed even when the operation raises an exception.

## What historical context changes in practice

The difference becomes clearer when looking at the actual audit
scenarios.

For a payments startup pitch, a generic model can flag the absence of a
revenue model and the risk of a solo founder. With relevant historical
memory, the audit can connect those concerns to a previous payments
startup in the records: one that grew quickly, never monetized, and
eventually shut down with a recorded five-lakh write-off. The system can
report the historical outcome and the lesson recorded at the time.

That comparison is useful because it is specific. It does not merely
repeat that monetization matters. It connects the current evaluation to
an earlier case and its recorded result. If no comparable case is found,
the application should say so and continue with a fundamentals-based
assessment.

The code-review path works similarly. A login query assembled through
string concatenation can be flagged for SQL injection risk. Without
memory, the advice is familiar: use parameterized queries. With a
matching historical record, the audit can bring back the earlier bug and
the fix that was recorded for it. The recommendation now has local
engineering context.

The runtime supervisor adds another kind of experience. A divide-by-zero
caused by an empty transaction set can match an earlier "guard empty
inputs" fix. The supervisor can use that precedent in its controlled
repair flow. If a crash has no similar resolved bug in memory, it stops
rather than treating the model's ability to write a patch as sufficient
justification.

These are different uses of the same memory bank, but they rely on the
same discipline: retrieved history must remain distinguishable from
generated analysis.

## What I would carry into another system

There are four practices I would reuse.

### 1. Treat provenance as application data

Do not rely on a model to infer whether a record is an investment, an
evaluation, a confirmed bug, or a proposed fix. Store that distinction
explicitly and preserve it when the record is recalled.

### 2. Make empty retrieval a first-class case

An empty result is meaningful. It means the system has no retrieved
precedent for the current question. Do not let a model fill that gap
with a plausible story and present it as history.

### 3. Put a budget between recall and generation

The memory system and the language model have different constraints.
Normalize, deduplicate, and cap recalled material before constructing
the prompt. Measure the usefulness of context, not just its volume.

### 4. Keep memory policy outside the model

The model can interpret records and reason about a new input. The
application should decide what counts as a historical match, how records
are labelled, and which outcomes qualify for retention. Those rules
should be testable without asking whether a prompt happened to persuade
the model.

## Memory is useful only when its history stays honest

The most valuable part of this project was not simply giving an audit
access to old information. It was making the relationship between stored
experience and generated conclusions explicit.

Hindsight provides the shared memory layer for business evaluations,
code reviews, and runtime incidents. The application makes that memory
usable: it bounds the recall passed to the model, labels what gets
written, and prevents generated content from being mistaken for
retrieved history.

That is the distinction I would preserve as the system grows. More
records and more retrieval results are not automatically better. The
goal is to give the model relevant experience without losing track of
where that experience came from or what it actually represents.

For a deeper introduction, see [Vectorize's explanation of agent
memory](https://vectorize.io/what-is-agent-memory), alongside the
[Hindsight GitHub project](https://github.com/vectorize-io/hindsight)
and its [documentation](https://hindsight.vectorize.io/). The
implementation lesson I would take away is simple: don't just give an
agent a memory. Make sure it knows the difference between remembering
something and making something up.

![Business Patch](images/Business Patch.png)
![Red Flags](images/Red Flags.png)
![Night Watch](images/Night Watch.png)

[Hindsight GitHub](https://github.com/vectorize-io/hindsight)

[Hindsight Documentation](https://hindsight.vectorize.io/)

[Vectorize Agent Memory](https://vectorize.io/what-is-agent-memory)