# Roadmap

## Next

**Calibrate quality thresholds from real outcomes.** The defaults in
`stockflow/imaging/quality.py` are derived from the maths, not from a labelled
dataset. Feeding back which submissions a marketplace actually accepted or
rejected would turn them from educated guesses into per-portfolio numbers.
This is the single largest accuracy win available.

**Confirm real rate limits.** Google no longer publishes a per-model table, so
`stockflow/limits.py` ships conservative estimates and self-corrects from 429
responses. Worth revisiting if an authoritative source reappears.

**Re-check the marketplace specs periodically.** `stockflow/marketplaces.py`
was verified against official contributor documentation in August 2026 and
cites its sources per rule. Marketplaces change these without notice, and a
wrong CSV header is rejected outright while a wrong category number is
accepted and silently mis-files the image. `tests/test_marketplaces.py` is
where a change should surface first.

**Emit Shutterstock's optional CSV columns.** Illustration, Mature Content and
Editorial (columns E–G) are documented and currently not written. Editorial in
particular changes how a submission is reviewed.

## Later

* Multi-agency upload support beyond Shutterstock and Adobe
* Portfolio database spanning folders, so dedupe and analytics work across an
  entire back catalogue rather than one batch
* Contributor analytics — acceptance rates by category, keyword performance
* Batch API support for large jobs, which has a separate and much larger quota
* Per-marketplace metadata profiles (keyword limits and category systems differ)
