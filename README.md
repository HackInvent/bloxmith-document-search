# Document Search

![Document Search](media/cover.png)

Index text supplied by your workflow and retrieve relevant, source-referenced
excerpts from persistent node-local storage. No remote provider, embeddings,
generated answers or automatic folder crawling.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

## Connect

1. Send documents to `documents`. Wait for `status` to confirm successful indexing.
2. Send plain search text or a query JSON object to `query`.
3. Route `matches` to your agent or viewer; check `status` for rejected requests.

Input ports are independent event inputs, not a join. Use upstream sequencing when
a query depends on a just-indexed document. Changing a query never replays latched
document or command inputs. Without new events, manual execution requires exactly
one populated input. Port names, not visual ordering, determine routing.

| Port | Direction | Contract |
| --- | --- | --- |
| `documents` | Input | One revisioned document or `{"documents":[...]}` |
| `query` | Input | Plain text or `{"text":"...", "document_ids":["manual"]}` |
| `command` | Input | Explicit `status`, `rebuild` or revision-checked `remove` |
| `matches` | Output | `{"results":[...]}`, one entry for each successful query |
| `status` | Output | `{"events":[...], "failed":0}`, ordered per-event acknowledgments |

Arrays preserve all replies when a runtime groups several new events into one
activation. Each entry has its zero-based `event_index`. Up to 16 events are
accepted per activation, sharing one timeout. Each event commits independently;
a later error does not roll back an earlier accepted event.

Expected input, capacity or index failures produce a status entry with
`status:"error"` and **no matches for that query**. They do not manufacture a
successful empty result. The task itself completes with diagnostics, so workflows
must inspect `status.events`/`failed`. Missing storage services or invalid block
configuration fail preparation/execution normally. No source text is written to logs.

## Document format

```json
{
  "id": "manual",
  "revision": 1,
  "title": "Installation guide",
  "metadata": {"language": "en"},
  "sections": [
    {"id": "page-3", "text": "Restart the service after installation.",
     "source": {"file": "guide.pdf", "sha256": "producer-supplied"},
     "page": 3}
  ]
}
```

Wrap the `sections` output of **Document Reader** in an identity/revision envelope
using the existing Python block. Source references are preserved as authored
data, not interpreted as paths or fetched URLs. `page`, `paragraph` and `section`
references, when present, must be positive integers. Section IDs default to their
one-based position. Titles and metadata are returned but are not searched.

A higher revision replaces a document atomically. An identical retry at the same
revision is idempotent. Different content at the same revision and older revisions
are refused. A batch of 1–100 documents either commits entirely or leaves the
corpus unchanged. Deleted identities retain a revision tombstone; re-creation
must use a revision higher than that tombstone.

## Search and references

```json
{"text":"restart service","document_ids":["manual"]}
```

A result contains `query`, `corpus`, `index_revision` and `items`. Every item has
`document_id`, `document_revision`, `section_id`, `title`, `metadata`,
`reference`, `excerpt`, `excerpt_truncated` and `score`.
A valid query with no match has `items:[]`. An empty document filter matches nothing.

The engine is SQLite [FTS5](https://www.sqlite.org/fts5.html), with its Unicode
tokenizer and diacritic removal. Choose `all_terms`, `any_term` or `phrase`.
Queries are literal Unicode word sequences, not SQL or raw FTS expressions;
punctuation separates words. There is no semantic similarity, fuzzy spelling,
stemming or hybrid vector retrieval. Search is confined to the configured corpus.

Scores are negated FTS5 BM25 scores: **higher ranks first within this query**.
They are neither probabilities nor comparable confidence thresholds across corpora.
Snippets contain up to 32 tokens, clipped to 4,096 characters with an explicit flag.
They are plain text, not trusted HTML. Escape them in any custom HTML renderer.

## Commands and persistence

```json
{"action":"status"}
{"action":"rebuild"}
{"action":"remove","expected_revisions":{"manual":1}}
```

Send these as separate events. Rebuild regenerates the FTS index from stored text;
it does not reread sources. Remove requires the exact current document revision
and only affects this corpus. Its acknowledgment includes the new tombstone
revision. Unknown, already removed or stale targets reject the entire removal
batch. This is not secure erasure; SQLite/free pages and external backups may
retain data. There is deliberately no implicit reset, migration or source-file deletion.

Storage comes exclusively from `context.services["get_block_storage_dir"]()`.
Each corpus has its own hashed SQLite filename, within that node's directory.
It survives Run/Stop/Reset under the framework's storage contract and is not
automatically cleaned up. Changing the corpus setting selects a separate index.
At most eight corpora are created per node; no eviction is performed.
`source_freshness:"not_checked"` explicitly means original documents may have changed.
`partial:false` describes a fully accepted batch, not completeness of the source library.

## Limits and setup

Python must have SQLite **FTS5** enabled. No extension is loaded or downloaded.
The implementation targets POSIX/Linux because it uses file locks. Files are
created with mode 0600; this is not encryption or a hostile-local-user sandbox.
Symlinked/hardlinked database files are refused. Unknown/corrupt schemas are
reported, never silently reset. All operations use bounded database lock waits.

Default limits: 1,000 retained document identities (including tombstones),
10,000 sections, 10 million text characters, 1 MiB per event, 10 excerpts and
a 10-second activation deadline. Advanced settings expose bounded increases.
A document has at most 500 sections; each section has at most 50,000 characters,
metadata and reference objects at most 4 KiB each. Queries: 512 characters/32 words;
filters: 100 IDs. Strict finite JSON, depth 20 and 50,000 values are enforced.
Database size is capped at 128 MiB per corpus (journals are additional temporary
storage). No automatic text retention policy is applied.

## Verification

Owned suites cover real FTS5 ranking/source round trips, atomic batches, revisions,
removal tombstones, concurrency, quotas, corrupt storage, cancellation, persistence
across Runs, both runtimes, managed/linked packages and responsive keyboard UI.
Tests run in isolated fixture storage, never your project's corpus.

Licensed under Apache-2.0.
