# Benchmark: Qwen2.5-VL 3B (Ollama, local) vs Gemini 2.5 Flash-Lite

**Date:** 2026-09-26/27 · **StockFlow:** `feature/local-vision-provider` (v5.1.0 + Ollama provider)
**Question:** can an RTX 3050 laptop run a local vision model well enough to power
StockFlow's stock-metadata workflow?

**Answer: not with Qwen2.5-VL 3B.** It is valid-JSON-reliable once runaway
generation is bounded, but its metadata is not trustworthy for submission even
after light review. Gemini remains the default provider.

## Setup

| | |
|---|---|
| Laptop | Intel i5-13420H, 16 GB RAM (15.7 usable), Windows 11 |
| GPU | NVIDIA GeForce RTX 3050 4 GB Laptop, driver 610.47, CUDA compute 8.6 |
| Ollama | 0.34.4, portable build, models on `D:` |
| Local model | `qwen2.5vl:3b` — 3.8B parameters, Q4_K_M, 3.2 GB on disk, Apache-2.0 |
| Placement | 3.5 GB loaded vs 3.2 GB free VRAM → **57% CPU / 43% GPU** split |
| Cloud model | `gemini-2.5-flash-lite` (free tier), StockFlow default settings |
| Image sent | 1024 px long edge JPEG (StockFlow's `api_max_edge`), same for both |
| Photos | 10 of the owner's own photographs (not committed): dragonfly head macro, two jumping spiders, dayflower with a small beetle, toad on bark, crescent moon + planet with a signature watermark, solitary tree with a signature watermark, chandelier, plant silhouette at sunset, and a phone gallery screenshot as a trap |

Runs used `--workers 1` and `--min-megapixels 1.5`; 7 of the 10 photos are below
Shutterstock's 4 MP minimum and would be rejected on upload regardless of metadata.

## Performance

| | Gemini (StockFlow) | Qwen 3B (StockFlow) | Qwen 3B (standalone, short prompt) |
|---|---|---|---|
| Wall time, 10 photos | 104.6 s | 300.5 s | 268 s |
| Per photo | **10.5 s** | 30.1 s | 23 s warm (+22.5 s first load) |
| Throughput | ~340/hour | ~120/hour | ~155/hour |
| Retries / failures | 0 / 0 | 0 / 0 | 0 / 0 |
| Tokens in / out | 14,174 / 4,217 | 22,013 / 1,326 | ~11,600 / ~1,400 |
| GPU utilisation | — | mean 24%, peak 100% | mean 24% |
| VRAM | — | peak 2.9 GB | peak 2.9 GB |
| CPU | local quality checks only | mean 73% | mean 69% |
| Free system RAM | — | **min 0.1 GB** | min 1.1 GB |
| GPU power | — | mean 16 W | mean 16 W |
| Per-image API cost | $0 on the free tier | $0 (electricity + a busy laptop) | $0 |

Generation runs at ~7–8 tokens/s because more than half the model is on the CPU.
Free RAM reaching 0.1 GB means the laptop is at its limit with this model plus
normal desktop applications.

## Quality — StockFlow runs, same 10 photos

Ground truth is a human reading of each photo at full resolution.

| Photo | Gemini | Qwen 3B |
|---|---|---|
| Dragonfly head | ✅ "yellow dragonfly's compound eyes and head" | ⚠️ adds "on green leaf"; **false watermark → rejected** |
| Jumping spider (face) | ✅ "fuzzy jumping spider … on a branch" (review: soft focus) | ⚠️ "on leaf" (it's a branch); **false watermark → rejected** |
| Jumping spider (leaf) | ✅ correct; ❌ keyword "insect" | ✅ correct title; **false watermark → rejected** |
| Dayflower + beetle | ✅ flower **and the beetle** | ⚠️ flower only, misses the beetle; 6 keywords |
| Toad | ✅ "small brown frog"; ❌ keywords "reptile", invented "pond, swamp" | ❌ "frog in dark **cave**" (invented); **false watermark → rejected** |
| Moon + signature | ✅ watermark caught; low quality (dark) | ❌ "**Full** moon and **lantern**" (crescent, no lantern); watermark caught |
| Tree + signature | ✅ **watermark caught** → rejected | ❌ **watermark missed → READY** |
| Chandelier | ✅ | ✅ |
| Plant silhouette | ✅ | ⚠️ vague title; **false watermark → rejected** |
| Gallery screenshot | ❌ treated as a moon photo (rejected only for darkness) | ❌ "partial **lunar eclipse**" (invented event) → **READY** |

| Metric | Gemini | Qwen 3B |
|---|---|---|
| Watermark/overlay flag correct | 9/10 (misses screenshot UI text) | **3/10** (5 false positives, 2 misses) |
| Invented facts (places, events, objects) | 1 photo (toad: pond/swamp) | 4 photos (cave, lantern, full moon, eclipse) |
| Taxonomy errors | spider tagged "insect"; toad tagged "reptile" | spiders typed "insect" in the standalone test; toad called gecko/spider |
| Species named | none — correctly generic | none — correctly generic ("unidentified …") |
| Mean keywords (asked for 40–50) | **45.7** | **6.2** — below Shutterstock's minimum of 7 on 5 photos |
| Keyword quality | broad, useful; some padding ("orb", "meadow") | short phrases, few concepts, no commercial terms |
| Commercial score | 35–75, discriminating | 75–85 for everything — not discriminating |
| Routed READY | 6 | 4 — including the screenshot and a watermarked photo |

Both CSVs were produced and structurally valid for both providers; the problem
is what went into them.

## Failures found and fixed during the benchmark

1. **Runaway generation.** Without an output cap, Qwen repeated keyword
   variants ("dragonfly insect macro close-up headshot body", …) until its
   4,096-token context filled: 394–581 s per image, invalid JSON. Fixed in the
   provider with `repeat_penalty` 1.15 and `num_predict` 1500; afterwards 20/20
   requests completed normally.
2. **Ollama grammar crash.** Bounding the keyword array with `minItems`/`maxItems`
   made Ollama 0.34.4 fail with `Unexpected empty grammar stack` and then reject
   subsequent requests. The provider deliberately sends no array-length bounds.

## Pipeline findings independent of the model

- **Small corner watermarks are unreadable at 1024 px.** In a separate set, a
  "REDMI NOTE 10 PRO" camera stamp and date on a 2088×4640 photo went unflagged
  by Gemini; at 1024 px the text is a few pixels tall. A full-resolution corner
  check would catch these without any model.
- **No "this is not a photograph" signal.** Neither model flagged a phone
  screenshot; the schema has no field for it and no rule routes it to review.
- **Resolution.** StockFlow's 4 MP floor matches Shutterstock's minimum; most
  phone-exported images in the sample fall below it.

## Recommendation

- **Keep Gemini as the default.** At ~1,500 photos/week (~215/day) the free
  tier covers the volume at no cost. Before relying on it for private photos,
  check the current Gemini API terms for how free-tier inputs may be used.
- **Keep the Ollama provider as an opt-in**, documented as experimental, for
  offline use or later hardware. Don't make it the default.
- **Qwen2.5-VL 3B is not stock-ready here:** inverted watermark judgement,
  a sixth of the keywords, invented objects and events, 3× slower, and it
  exhausts system RAM. A 7B-class model would not fit this GPU at all.
- **Fedora server:** no. No usable GPU, 7.6 GB RAM, no AVX2 — slower than this
  laptop by a wide margin. Keep it for storage and backups.
- The two pipeline gaps above (corner watermarks at full resolution, a
  screenshot/non-photo signal) improve every provider and are worth doing
  before any further model testing.
