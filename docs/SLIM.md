# Switching to the slim image

The default stack uses the **full** image, which bundles the local BGE embedder
and MiniLM cross-encoder. That is why it needs about 2 GB of RAM and a 3.7 GB pull.

The **slim** image is about 500 MB and runs in roughly 1 GB, but it ships no local
models. It is **not a drop-in swap**: with no embeddings provider configured it
cannot store anything, and with no reranker configured recall quality drops.

## What you must add

Change the tag, then configure both providers:

```bash
# in .env
HINDSIGHT_VERSION=latest-slim

# embeddings, required
HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai
HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=sk-xxx
HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL=text-embedding-3-small

# reranker, required
HINDSIGHT_API_RERANKER_PROVIDER=cohere
HINDSIGHT_API_RERANKER_1_COHERE_API_KEY=xxx
```

Those variables are not wired into `docker-compose.yml`, because the default
stack does not need them. Add them to the `hindsight` service's `environment`
block first, or they will not reach the container.

## Two caveats worth knowing

- **Changing the embedding model invalidates existing vectors.** Vectors produced
  by the full image's BGE embedder are not comparable to OpenAI vectors, and the
  dimensions differ. Switching an existing stack means re-embedding everything.
  Treat it as a fresh start unless you have a migration plan.
- **`flashrank` is a lighter middle ground** than a hosted reranker. It runs
  in-process without a GPU or a second API key:
  `HINDSIGHT_API_RERANKER_PROVIDER=flashrank`

## When it is worth it

Mainly when RAM is the constraint, or when you already pay for hosted embeddings
and would rather not run models locally. On this Mac, with the Colima VM sized at
8 GB, the full image is the simpler choice: one credential instead of three, and
no re-embedding decision to make.
