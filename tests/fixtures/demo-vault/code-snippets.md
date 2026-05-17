---
title: Code Snippets
tags:
  - code
  - reference
---

# Code Snippets

A small grab-bag of fenced code blocks for the chunker fixtures.

## Bash

```bash
set -euo pipefail
echo "hello, datacron"
```

## SQL

```sql
SELECT chunk_id, bm25(chunks_fts) AS score
FROM chunks_fts
WHERE chunks_fts MATCH 'datacron'
ORDER BY score
LIMIT 10;
```

> A blockquote in the middle of the note, for quote-chunk coverage.
