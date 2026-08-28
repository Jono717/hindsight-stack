# Curating memories: invalidation

A memory system accumulates claims that were true when written. Anything about
mutable config goes stale silently, and recall keeps returning it with full
confidence. Invalidation is how you retire those.

## The mechanism

```
PATCH /v1/default/banks/{bank}/memories/{memory_id}
{ "state": "invalidated", "reason": "why it is no longer true" }
```

It is a **soft retire**, not a delete. The row stays, `invalidated_at` and
`invalidation_reason` are set, and the text is still readable by id. What changes is
that it is excluded from recall and from consolidation.

That is the right default. A hard delete loses the fact that you once believed it,
which is often the thing worth knowing.

## Doing it

```bash
make find BANK=my-vault QUERY="the stale claim"
make invalidate BANK=my-vault ID=<uuid> REASON="superseded by X on <date>"
```

`find` prints ids next to text so you can pick the right one. `invalidate` refuses
to run without a `REASON`, because an invalidation with no reason is unreadable six
months later, and the reason is the only part that explains itself.

## Two things that will trip you up

**Recall returns the same fact more than once**, typically as a `world` fact and an
`observation`. They are separate rows with separate ids.

**Only some of those ids are patchable.** `PATCH /memories/{id}` operates on memory
units. Observation ids come back from recall but are not memory units, and patching
one returns:

```
{"detail":"Memory unit '<id>' not found"}
```

That is not a problem in practice: invalidating the underlying `world` fact removed
its observation from recall too. Verified 2026-08-28 across three different query
phrasings, all returning zero stale hits afterwards. So **target the `world` row**
and let the derived observation follow.

## Worked example

The bank held `Default Hindsight model is anthropic.claude-sonnet-5`, written when
that was true. The model has since moved twice, to haiku 4.5 and then to
`gcp:gemini-3.1-flash-lite`.

```bash
make find BANK=my-vault QUERY="default Hindsight model"
# 72ee3057-...  [world]        Default Hindsight model is anthropic.claude-sonnet-5 ...
# adb322cf-...  [observation]  Default Hindsight model is anthropic.claude-sonnet-5 ...

make invalidate BANK=my-vault ID=72ee3057-523e-4123-b074-a181fea01873 \
  REASON="Superseded 2026-08-28: default model moved sonnet-5 -> haiku-4.5 -> gcp:gemini-3.1-flash-lite"
```

Both copies stopped appearing in recall.

## Do not over-invalidate

When searching for that stale claim, the results also included several memories
mentioning sonnet-5 that were **not** stale:

- *"Pin claude model to `aws:anthropic.claude-sonnet-4-6`, never sonnet-5"* — about
  **graphify**, a different system, and still true
- *"hres-hpc-dev01-fsxn rebuild completed using Claude Code with Sonnet 5"* — a
  record of something that happened, and history does not expire

The distinction: a claim about **current state** goes stale; a record of a **past
event** does not. Text matching cannot tell these apart, so read before invalidating.

## Writing memories that age better

The stale entry existed because a **current config value** was stored as a fact.
Those rot on every change. Reasoning ages far better:

- Rots: *"The default model is claude-sonnet-5."*
- Keeps: *"A gateway that fronts several providers gives the same model a different
  id per path, so the provider, base URL and model id must always be changed
  together."*

The second is still true across all three model switches. For values that genuinely
change, the live config is the source of truth, not memory: `make api-key`,
`docker exec hindsight-app printenv HINDSIGHT_API_LLM_MODEL`.
