# Ways to run StockFlow

StockFlow always does the same work — duplicates, local quality measurement,
sorting into folders, embedded IPTC/XMP, Shutterstock and Adobe Stock CSVs. What
changes is **where each photo's title, keywords and flags come from**.

| Mode | Metadata from | Cost | Throughput | Unattended | Files here |
|---|---|---|---|---|---|
| **Gemini, free tier** | Google's API | $0 | limited by the daily quota — can be as low as 20 photos/day per project | yes | [`free-tier/stockflow.json`](free-tier/stockflow.json) |
| **Gemini, paid tier** | Google's API | ~$0.0003 per photo with `gemini-2.5-flash-lite` (measured, Sept 2026 prices) | hundreds per hour | yes | [`paid-tier/stockflow.json`](paid-tier/stockflow.json) |
| **Claude Code (sidecar)** | an AI assistant or a person, writing `metadata/<file>.json` | no API bill | as fast as the writer | no | [`claude-code/CLAUDE.md`](claude-code/CLAUDE.md) |
| **Local model (Ollama)** | a vision model on your own machine | electricity | hardware-bound | yes | see the README; experimental — read [the benchmark](../docs/benchmarks/2026-09-local-vision-qwen2.5vl-3b.md) first |

## Using a config file

Copy one into the photo folder as `stockflow.json`; StockFlow picks it up
automatically. Flags on the command line still win. Keep `GEMINI_API_KEY` in
the environment — never in the config file.

**Free tier:** one worker and batches of 20. StockFlow learns the project's real
daily limit from the first quota error and plans later runs around it. On the
free tier Google may use your inputs to improve its products; the paid tier
does not.

**Paid tier:** the `rpm`/`rpd` values are deliberately conservative. Look up your
project's actual limits in AI Studio and raise them.

## Using the Claude Code mode

1. Copy [`claude-code/CLAUDE.md`](claude-code/CLAUDE.md) into your photo folder.
2. Open Claude Code in that folder and ask it to work the batch. It reads
   `CLAUDE.md`, prepares previews with `--prepare-sidecars`, looks at each photo
   and its full-resolution corners, writes one JSON file per photo, and runs
   StockFlow with `--provider sidecar`.

The same mode works for anyone writing metadata by hand, or any other tool: one
JSON file per photo, in the format of [`claude-code/example-metadata.json`](claude-code/example-metadata.json),
validated exactly like a model's response. Photos without a file yet simply
stay pending.
