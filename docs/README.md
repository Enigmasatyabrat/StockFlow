# StockFlow documentation

Four documents, in the order most people need them.

| | |
|---|---|
| **[why.md](why.md)** | What problem this solves and why it was built this way. Start here if you're deciding whether the tool is for you. |
| **[reference.md](reference.md)** | Every flag, folder, output file and exit code. The page to keep open while using it. |
| **[troubleshooting.md](troubleshooting.md)** | When something goes wrong, or looks wrong. Ordered by how often each problem actually happens. |
| **[architecture.md](architecture.md)** | How the code is laid out and why the awkward decisions were made that way. For anyone changing it — including me in six months. |

The [main README](../README.md) is the short version: install, run, what comes
out the other end.

## The short version

StockFlow takes a folder of photos and sorts them into upload-ready buckets. It
measures technical quality on the full-resolution file, asks a vision model to
judge commercial prospects, writes searchable metadata into the file itself,
and produces CSVs in the exact formats Shutterstock and Adobe Stock demand.

Nothing is deleted. Every decision comes with a written reason. And
`--dry-run` will show you the whole thing before it touches a single file:

```bash
stockflow "D:\Photos\batch_01" --dry-run
```

## Things worth knowing before you trust it

- **The commercial scores are guidance, not a verdict.** Marketplace reviewers
  make the real decision.
- **The quality thresholds are starting points**, derived from the maths rather
  than from a labelled dataset. Local gates are off by default for exactly that
  reason — everything is measured and reported, nothing is rejected on those
  numbers unless you ask.
- **Adobe's `Releases` column is left blank.** It wants the filenames of
  release documents you already uploaded to their portal, which this tool has
  no way of knowing. Fill it in yourself for anything in `05_NEEDS_RELEASE/`.
- **Rate limits are estimates.** Google stopped publishing a per-model table,
  so the defaults are conservative and self-correct from what the API actually
  reports.
