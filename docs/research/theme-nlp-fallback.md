# A language-processing judge for themes, without AI

*Design note, 2 October 2026. Parked: nothing here is built. Technical
scope only.*

Whether a theme-based crawl can judge pages with established
natural-language-processing (NLP) methods when no AI judge is configured,
what that would take, and what must be measured before choosing.

## Contents

- [Status](#status)
- [Where SWM stands today](#where-swm-stands-today)
- [The options](#the-options)
- [Where it would fit](#where-it-would-fit)
- [Established libraries](#established-libraries)
- [What the stemmer check showed](#what-the-stemmer-check-showed)
- [Trade-offs specific to SWM](#trade-offs-specific-to-swm)
- [Recommendation](#recommendation)
- [Before building: the comparison](#before-building-the-comparison)

## Status

Parked. The ideas and the checks below are recorded so the work can pick
up from here. Sizes and licences were read from the packages themselves;
the stemmer outputs were run; everything marked *estimated* was not
measured.

## Where SWM stands today

- **The rules judge already does light language processing.** It folds
  case, tolerates English plurals and Arabic clitics and endings (مكتبة,
  مكتبات and المكتبات match), normalises Arabic letter forms, and scores
  by where a term appears: headline 3, section, tags or description 2,
  each mention in the text 1 (up to 5), an address rule 3. A page is
  accepted at the minimum score (3 by default, per job).
- **It only knows the listed terms.** A page about Ethiopia that only
  says "Addis Ababa" or "Tigray" scores 0.
- **The theme's brief and examples are read only by the AI judge.**
  Without AI, everything the curator wrote describing the theme is unused.
- **"No AI" need not mean "no model".** SWM can already use a local model
  through the OpenAI-compatible setting (for example Ollama), so no cloud
  service is required. An NLP fallback is for machines that cannot run a
  model, or where fast, repeatable judgements are wanted.

## The options

From lightest to heaviest.

1. **Better word matching.** Proper stemming, so "archives", "archiving"
   and "archival" meet; BM25 weighting, so rare, telling words count more
   than common ones and long pages do not win by length; and scoring the
   page against the brief and the example pages as well as the terms, so
   the brief matters without AI. Fully explainable ("matched: Tigray,
   weight 4.1"). It still matches words, not meaning: a synonym not
   listed still misses.
2. **Learning from the curator's decisions.** Accepted pages, rejected
   pages, and the pages chosen for recrawl in the Selection report are
   labelled examples. A small classifier (logistic regression or naive
   Bayes over word weights) learns what separates them and can show the
   words that tipped each decision. Useless on a theme's first crawl;
   better with every review, which fits the report-and-recrawl workflow.
3. **Meaning-based similarity.** A small local embedding model turns each
   page, the brief and the examples into vectors and compares them by
   meaning, so "Addis Ababa" sits near "Ethiopia" unlisted. Multilingual
   models can match an English brief to Arabic pages. Cost, *estimated*:
   a 100–500 MB model and tens of milliseconds per page on an ordinary
   CPU, and it needs a runtime that avoids the 1–2 GB of the usual
   machine-learning stack (ONNX rather than PyTorch). The reason shown is
   weaker: "similarity 0.71 to the brief (0.60 needed); closest example
   …". Amharic is weakly supported in many such models.
4. **Zero-shot classifiers and named-entity recognition.** Heavier and
   slower, and close to what a small local AI model already does. Not
   worth adding.

## Where it would fit

- **Settle the in-between pages.** Run it only on pages scoring above 0
  but below the minimum: the "Not enough evidence" rows of the Selection
  report. This is the slot the AI judge fills under its tie-break policy.
- **Stand in when the AI cannot.** No AI configured, the AI's call
  allowance spent, or a call failed: today these fall back to the rules
  alone.
- **Triage links before fetching.** Comparing link text with the brief is
  cheap and would save requests.
- **Report the same way.** The Selection report would show its score
  against its threshold, like the rules' "1 / 3", and the theme summary
  would record the method, library versions and threshold.

## Established libraries

Read from the published packages (wheels for Python 3.11, x86-64).

| Library | Gives | Licence | Size and needs |
|---|---|---|---|
| snowballstemmer 3.1.1 | The Snowball stemmers, 36 languages incl. Arabic, Persian, Hindi; not Amharic | BSD-3-Clause | 104 KB wheel, pure Python, no dependencies |
| PyStemmer 3.1.0 | The same stemmers, compiled | MIT/BSD | 744 KB wheel, no dependencies |
| bm25s 0.3.11 | BM25 weighting and ranking | MIT | 75 KB wheel, needs numpy |
| rank-bm25 0.2.2 | BM25 | Apache 2.0 | 8 KB wheel, needs numpy; maintenance not checked |
| scikit-learn 1.9.1 | TF-IDF weighting, and the classifiers for option 2 | BSD-3-Clause | 9.3 MB wheel, needs numpy, scipy and others; about 60 MB installed (*estimated*) |
| NLTK 3.10.3 | Broad toolkit, incl. Arabic stemmers | Apache 2.0 | 1.8 MB wheel; many features need separate data downloads, awkward offline |
| spaCy 3.8.16 | Full language pipelines | MIT | 35 MB wheel plus about 20 packages and a model per language. Too heavy |
| Stanza 1.15.0 | Full pipelines | Apache 2.0 | Requires PyTorch (gigabytes). Too heavy |

## What the stemmer check showed

Run on 1 October 2026.

**English, Snowball:** library and libraries → `librari`; archive,
archives, archiving, archival → `archiv`; librarian stays `librarian`, as
stemmers intend. Good.

**Arabic: established does not mean better here.**

| Word | Snowball | NLTK ISRI | NLTK ARLSTem2 |
|---|---|---|---|
| مكتبة (library) | مكتب | كتب | مكتب |
| مكتبات (libraries) | مكتبا | كتب | مكتب |
| المكتبات (the libraries) | مكتب | كتب | مكتب |
| والمكتبة (and the library) | والمكتب | كتب | مكتب |
| ثقافة / الثقافية (culture / cultural) | ثقاف / ثقاف | ثقف / ثقف | ثقاف / ثقاف |
| إثيوبيا / الإثيوبية (Ethiopia / Ethiopian) | — | ثيب / ثيب | ثيوب / اثيوب |

- **Snowball** is inconsistent (مكتبات → مكتبا, but المكتبات → مكتب) and
  leaves the "and the" prefix on والمكتبة. SWM's own matching already
  handles all of these.
- **ISRI** reduces to the three-letter root: library becomes كتب, the
  root shared by book, writer and office. Far too broad for selection.
- **ARLSTem2** is consistent on the library forms, but gives library and
  office (مكتب) one stem, and splits إثيوبيا from الإثيوبية. SWM's letter
  normalisation applied first would likely mend the split (not tested).
- **Amharic** is covered by none of these.

## Trade-offs specific to SWM

- **Footprint.** The core install has four dependencies. Anything here
  should be an optional extra (for example `theme-nlp`), like the AI
  judge's `theme-ai`; without it, SWM keeps today's rules.
- **Provenance.** Record the method, library versions and thresholds in
  `theme-summary.json`, as the AI judge's model and prompt hash are
  recorded. Unlike an AI model, these methods answer the same way every
  time, which suits an archive.
- **Thresholds need calibrating per theme.** A distribution of scores in
  the Selection report would help a curator set them.
- **Arabic stays on SWM's own normalisation and matching,** in front of
  any library stemmer.

## Recommendation

- **Option 1 first**, with snowballstemmer for English and other
  Latin-script languages, SWM's Arabic handling kept in front, and BM25
  over the terms, the brief and the examples.
- **For the weighting**, either bm25s (smallest: adds only numpy) or
  scikit-learn (about 60 MB, but the same library then carries option 2,
  learning from the curator's decisions).
- **Option 3** is the real gain on wording the terms do not cover, if the
  comparison below shows option 1 is not enough.
- Optional install throughout.

## Before building: the comparison

Decide on evidence, not on the table above:

1. Take a real themed crawl's `selection.jsonl` (the Ethiopia job, with
   its Arabic pages, if available).
2. Label 100–200 of its pages by hand: relevant or not.
3. Score each approach (rules alone; option 1 with each stemmer and
   weighting; option 3 with one small multilingual model) on how many
   relevant pages it catches and how many irrelevant ones it lets in.
4. Note time per page and install size alongside.

This runs outside SWM and changes nothing in it.
