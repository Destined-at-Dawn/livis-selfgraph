# Livis SelfGraph adapter

A small, dependency-free Python adapter that turns an approved public web page into the `insert_text` payload used by [SelfGraph](https://github.com/hydra-db/selfgraph). It is designed for the Livis integration while remaining independently testable.

## What it does

- validates HTTP(S) URLs against an exact host allowlist
- rejects loopback, private, link-local, reserved, multicast, and local-only hosts
- refuses embedded credentials and cross-host redirects
- extracts visible HTML or plain-text content with a stable `source_id`
- builds SelfGraph `insert_text` arguments with source metadata

The adapter stores no credentials and has no runtime dependency outside the Python standard library. The Livis application owns MCP process startup and user-facing persistence.

## Install and test

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Minimal usage

```python
from livis_selfgraph import extract_document, insert_arguments

document = extract_document(
    "https://example.com/notes",
    "<article><h1>Notes</h1><p>Graph-ready text.</p></article>",
)
arguments = insert_arguments(document, "default")
```

`fetch_document()` is available when the caller has already selected the approved host. It limits response size, accepts HTML/XHTML/plain text, checks DNS results for private addresses, and does not follow redirects.

## Upstream

SelfGraph is developed by [hydra-db/selfgraph](https://github.com/hydra-db/selfgraph). This repository contains the Livis-side website adapter and its tests.
