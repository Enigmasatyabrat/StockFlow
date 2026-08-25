# Why StockFlow exists

## The problem

I shoot a lot. Over the years that has turned into an archive spread across
folders and drives — more images than I can realistically go through by hand.
Somewhere in there is a large amount of work that would sell on Shutterstock,
Adobe Stock and similar marketplaces. The barrier was never the photography.
It was everything that has to happen *after* the photography.

For a single image to be submittable, someone has to:

- decide whether it is technically good enough — sharp, clean, well exposed
- decide whether it is *commercially* interesting, which is a different
  question entirely
- write a title that buyers will actually search for
- write 40–50 keywords, ordered by relevance, with no duplicates
- pick a category
- notice whether there is an identifiable person, building or logo in frame
  that would need a release
- notice whether it is a near-duplicate of something already submitted
- embed all of that into the file as IPTC and XMP metadata
- convert it to something the marketplace accepts
- put it in a CSV in the exact format the marketplace demands

That is maybe five to ten minutes per image if you are quick and know what you
are doing. At a thousand images it is weeks of work. At my archive size it is
not a task, it is a second job — and it is the least interesting part of
photography.

So the whole thing stalls. The photos sit on the drive. That is the actual
problem StockFlow solves: **not "can this be done", but "will it ever
realistically get done".**

## What I wanted

One command. Point it at a folder, walk away, come back to sorted folders and
a CSV I can upload.

Specifically:

- Every image ends up in exactly one place, with a written reason I can read
- Nothing gets deleted, ever
- The originals are kept
- It can be stopped and restarted without losing work or redoing paid API calls
- It tells me what it could not decide, instead of guessing silently

The last two matter more than they sound. A tool that processes a large archive
*will* be interrupted — quota runs out, the machine sleeps, I close the laptop.
If that loses progress or, worse, silently strands files somewhere I never look
again, the tool is worse than useless. It has quietly damaged the archive it
was supposed to organise.

## Why the technical measurement is done locally

This is the part of the design I care most about.

The obvious approach is to hand the image to a vision model and ask "is this
good?". But you cannot send a 24-megapixel file to an API — you downsample it
first, typically to around 1024 pixels, and compress it. By that point the
sharpness and sensor noise are physically gone. Asking the model to grade
technical quality on that image is asking it to grade information that no
longer exists.

And technical faults are exactly what marketplaces reject for.

So StockFlow measures focus, noise, exposure clipping and contrast itself, on
the full-resolution file, with numpy — and hands those numbers to the model as
ground truth. The model then does what it is genuinely good at: judging whether
a subject is commercially interesting and describing it in the language buyers
search.

Each side does what it is actually capable of.

One detail I insisted on: the focus score is a **high percentile across the
frame**, not an average. A sharp subject against a soft background is good
photography, not a defect. Averaging would penalise exactly the shallow
depth-of-field work that sells best. The tool has to understand the difference
between *blurry* and *bokeh*, or I would not trust it near my portfolio.

## Why the thresholds do not reject anything by default

Blur, noise and clipping are always measured and always reported — but nothing
is rejected on those numbers unless you explicitly ask with `--min-blur` and
friends.

That is deliberate. The thresholds are derived from the mathematics, not from a
labelled dataset of my accepted and rejected submissions. They are educated
starting points. A tool that silently bins my photographs based on a number
someone guessed is not a tool I would run twice.

Measure everything, report everything, reject only what I asked it to.

## What it is not

StockFlow does not decide whether a photo gets accepted. Marketplace reviewers
do. A high score is an indication that something is worth submitting, nothing
more. The disclaimer in the README is not legal boilerplate — it is the honest
description of what a score means.

It also does not replace judgement about releases. It flags images that appear
to contain an identifiable person, building or trademark and routes them to a
folder for me to look at. Deciding whether a release is actually needed is a
legal question about a specific photograph, and that stays with me.

## Where it goes next

The single biggest improvement available is calibration: feeding back which
submissions were actually accepted or rejected, and tuning the thresholds to
*my* portfolio rather than to a general guess. That turns the numbers from
educated estimates into something earned from real outcomes.

The companion project, [PostFlow](https://github.com/Enigmasatyabrat/PostFlow),
takes the sorted output and handles social posting. The two are deliberately
separate: StockFlow sorts and writes CSVs, PostFlow posts. Neither should have
to know how the other works.
