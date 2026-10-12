# mendwright's local selector model

This folder builds `mendwright-selector`, a small model that runs in [Ollama](https://ollama.com) on your own machine and answers one question for mendwright: which element on this page is the one a broken step meant, if any.

It is Qwen2.5-1.5B-Instruct, fine-tuned with QLoRA on synthetic redesigns, then merged and quantized to a GGUF file of about 1 GB. Nothing in here is part of the installed package. The package side is [`mendwright/candidates.py`](../mendwright/candidates.py) and [`mendwright/providers/local.py`](../mendwright/providers/local.py).

## What the model does

The model never sees a whole page and never writes an XPath. When every selector of a key stops matching, mendwright first tries its fingerprint tier, which needs no model. If that is not confident, `LocalModelProvider` builds a short task from the captured page with `mendwright.candidates`:

```text
Step
key: login.submit
role: button
old selector: #sign-in
it was: button "Sign in" id="sign-in" | main>form | within form < main
page: /login (where it was last found)

Candidates (3 buttons)
1. button aria="Open menu" | header
2. button "Forgot password?" | main>form
3. button "Log in" class="primary" | main>form
```

The step comes first: its key, role, old selectors, expected text and `intent=` if set, the fingerprint mendwright saved the last time the selector matched, and the page path now and where the element was last found.
Then come at most 40 elements of the step's role, in page order. Each line has the tag, visible text, label, id, name, test id, aria-label, placeholder, title, alt, link path, class names a person chose, the row, card or heading it sits under, its landmark (header, nav, main, form, dialog, footer) and whether the page hides it.
When the page has more than 40 elements of that role, the 40 that share the most words with the step are kept.

The model answers `{"pick": 3}`, or `{"pick": null, "why": "removed"}`. A null pick gives one of three reasons: `removed` when the element is not on the page, `ambiguous` when several fit and nothing tells them apart, and `moved` when the step now belongs to another page.

mendwright then builds an XPath for the picked element in code: its id, test id, name or aria-label, a label or text anchor, or a row, card or heading around it, and never a bare position. It sends that XPath and up to three others for the same element through the same checks as any proposal. When those checks refuse the pick because the element is not visible or not unique on the live page, the model is asked once more without it. For any other refusal it does not guess a second time.

## Hardware used

- Laptop: Intel Core i7-13620H (16 threads), 32 GB RAM, Windows 11
- GPU: NVIDIA GeForce RTX 4050 Laptop, 6 GB (training peaked at 3.8 GB of it); the laptop was in use for other things during training
- Python 3.12, torch 2.11 with CUDA 12.8, transformers 5.19, peft 0.21, bitsandbytes 0.50
- Ollama 0.40.3, llama.cpp for the GGUF conversion and quantization

## Reproducing it

Run these from the repository root. Paths below are examples; the scripts take them as arguments.

```bash
python -m venv ../train-venv
../train-venv/Scripts/pip install torch --index-url https://download.pytorch.org/whl/cu128
../train-venv/Scripts/pip install transformers peft bitsandbytes accelerate lxml cmake ninja
../train-venv/Scripts/pip install -r ../llama.cpp/requirements/requirements-convert_hf_to_gguf.txt

# 1. Data: about 5 minutes on 12 processes
python training/build_dataset.py --out ../data/r3 --train 30000 --val 2000 --test 3000 --start 2000000

# 2. Training: QLoRA, about 6 hours on the laptop above (resume with --resume)
../train-venv/Scripts/python training/train.py --data ../data/r3 --out ../runs/r3 --lr 1.5e-4

# 3. Evaluation with greedy decoding, on the adapter
../train-venv/Scripts/python training/evaluate.py --data ../data/r3 --split test --adapter ../runs/r3/adapter

# 4. Merge, convert, quantize and create the Ollama model
../train-venv/Scripts/python training/export.py --adapter ../runs/r3/adapter --out ../export/r3

# 5. The shipped model through Ollama, the part of the test split that reaches the model,
#    and the benchmark
python training/evaluate.py --data ../data/r3 --split test --ollama mendwright-selector
python training/evaluate.py --data ../data/r3 --split test --reaches-model --ollama mendwright-selector
PYTHONPATH=. python benchmarks/run.py --provider local --model mendwright-selector
```

The shipped model did not come from one such run but from three rounds, each started from the last one's adapter with `--init` (see [Training](#training)). Step 2 above is the plain single run on the final data; it has not been measured on its own.

`train.py` writes a checkpoint (adapter, optimizer state and position) every 100 optimizer steps, so an interrupted run goes on with `--resume`.

`export.py` first asks `ollama create --quantize q4_K_M` to import the merged safetensors. Ollama 0.40 quantizes safetensors only to its own int4 and int8 formats, so the script then converts the merged model to GGUF f16 with llama.cpp's `convert_hf_to_gguf.py`, quantizes it with `llama-quantize` and imports that file. On Windows without Visual Studio, `llama-quantize` builds with MSYS2's MinGW gcc and the pip packages `cmake` and `ninja`:

```bash
cmake -S llama.cpp -B llama.cpp/build-mingw -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ -DGGML_NATIVE=OFF -DLLAMA_CURL=OFF \
  -DGGML_OPENMP=OFF -DBUILD_SHARED_LIBS=OFF
cmake --build llama.cpp/build-mingw --target llama-quantize
```

## The data

All examples are synthetic and generated here. No page was scraped, no cloud model wrote or labelled anything, and [`benchmarks/cases`](../benchmarks/cases) is never read: it stays the final test.

[`gen_scenarios.py`](gen_scenarios.py) builds a page before a redesign (v1), the page after it (v2) and a step: a key and the selector a test author might have written for one control on v1. [`pages.py`](pages.py) has the page families and [`lexicon.py`](lexicon.py) the words.

- Page families: login, sign-up, password reset, two-step verification, checkout, cart, product grid with filters, settings (profile, notifications, security), a members table with row actions, a dashboard, a confirmation dialog over a page, a landing page, and site chrome (header, mega menu, hidden mobile menu, footer) on all of them. Cookie banners from nine consent managers and newsletter popups appear over some pages.
- Markup styles: BEM, utility classes, CSS-module and styled-components hashes, Emotion, Bootstrap, plain or none; semantic, generated or missing ids; `data-testid`, `data-test`, `data-qa`, `data-cy`; labels by `for`, wrapping, `aria-label`, `aria-labelledby`, placeholder or a nearby span; Angular Material, MUI, Ionic, Chakra and custom-element wrappers; buttons as `button`, `input`, `a role=button` or `div role=button`.
- Words: 177 controls with English synonyms and German, French, Spanish, Portuguese, Italian and Dutch translations.
- Redesigns, one to three per example: id renamed, removed or generated; class churn; new wrappers; the control moved to another region; fields reordered; the label reworded or translated; a tag change that keeps the role; attribute churn (name, test id, aria-label); near-duplicates where the row or card decides; a hidden copy; destructive or similar decoys next to the control; a route rename; the control seen on another page of the site (chrome).
- Negatives (about 30%): the control removed; removed with a destructive or merely similar control in its place; duplicates that nothing tells apart; the step moved to another page with a look-alike control on it; an interstitial page (session expired, verify your email, access denied, age check, "are you still there?").
- One positive in five gets a twin negative: the same redesign with the control gone.
- Some steps carry an `intent=`, and some are a key met for the first time with only `intent=` and `role=`.

[`build_dataset.py`](build_dataset.py) runs every scenario through mendwright itself: `mendwright.open()` on a `StandInPage` at the v1 URL, `heal.locator()` so the selector matches and mendwright records the role, the page and the fingerprint, then v2 at its URL and `heal.locator()` again with a provider that keeps the bundle. The fingerprint tier is turned off for that second call, so every scenario reaches the provider, and the script records what the tier would have done. The task is then `format_task(bundle, extract(bundle))`, the same functions the provider calls, and the label is the truth's number in the list.

One step in five is shown without its fingerprint, as for a key healed before mendwright recorded one, but only when its key, selectors or text still say what the element is. A step with no fingerprint whose key and selectors say nothing (`step3`, a positional selector) is labelled `ambiguous`.

The test split uses three page families the training data never has (search results, a sign-up wizard, a blog article) for 60% of its examples, and product, people, project and brand names that training never sees.

| Data | Generator | Train | Val | Test | Negatives | Truth in the candidate list |
|---|---|---|---|---|---|---|
| round 1 | commit 0b23304, seeds from 0 | 34,000 | 2,000 | 3,000 | 30% | 99.97% (train), 99.86% (test) |
| round 2 | commit 059a9b2, seeds from 1,000,000 | 30,000 | 2,000 | 3,000 | 29% | 99.94% (train), 99.91% (test) |
| round 3 | commit 82fc493, seeds from 2,000,000 | 30,000 | 2,000 | 3,000 | 29% | 99.97% (train), 99.95% (test) |

"Truth in the candidate list" is the share of positives whose right element survived the cut to 40 candidates; the few that did not were dropped and counted. Round 3 has 20 page families for training (the 12 above plus contact, pricing, inbox, booking, files, a task board, a product page, an order list and an order review page). Its test split has 3,000 examples, 1,742 of them on the three held-out families. In the round 3 test split, 1,632 steps are ones the fingerprint tier would not heal on its own (no fingerprint, or no confident match), so they are what reaches the model at run time.

## Training

QLoRA on Qwen2.5-1.5B-Instruct: 4-bit NF4 with double quantization, bf16 compute, LoRA rank 16 and alpha 32 with dropout 0.05 on every linear layer (18.5M trainable parameters), the base model's chat template, and the loss on the answer and its `<|im_end|>` only. Batch 1 with 16 steps of gradient accumulation, AdamW, a learning rate of 2e-4 (1.5e-4 in later rounds) with 3% warmup and a cosine decay, and gradient checkpointing. Only the logits of the answer positions are computed, which keeps a run under 3.8 GB of GPU memory. In the round 3 data, prompts have a median of 329 tokens and a 99th percentile of 1,640; the 11 of 30,000 over the 2,048-token limit were left out.

Three rounds, each fixing what the last one's errors showed:

1. Round 1, from the base model on the round 1 data. Stopped at step 900 of 2,125 (14,400 examples), when error analysis at step 600 showed generator bugs: mega-menu links and steps on other pages of a flow labelled as moved although the same control was there, a first-use step labelled as no element although the page had it, controls moved into another row, filter checkboxes sharing a meaning with "in stock only", and keys such as `step3` that the stop-word check let through.
2. Round 2, from round 1's adapter on the corrected generator (and twice the share of moved steps), learning rate 1.5e-4. Stopped at step 500 (8,000 examples) to add eight page families and fix what the benchmark showed the candidate lines lacked: rows that differ only by a variant (`Canvas cap · Red / M`), avatar initials taken for a row's name, and an Apply button that only the code box before it tells apart.
3. Round 3, from round 2's adapter on the round 3 data, one full epoch (1,874 steps, 357 minutes on the shared laptop). This is the shipped model.

Before the first round, a pilot of 1,600 examples on an earlier format checked the whole path from data to Ollama.

## Results

All numbers are greedy decoding through Ollama with the q4_K_M model, the way `LocalModelProvider` asks it. A positive is a step whose element is on the page: the pick is correct, wrong (another element) or missed (no pick). A negative is a step whose element is not: the model should give no pick. Every reply was valid JSON in every run.

| Model | Split | Positives correct | Wrong picks | Missed | Negatives declined |
|---|---|---|---|---|---|
| round 1, step 600 | round 1 val, first 500 | 96.7% | 0.9% | 2.4% | 93.9% |
| round 1, step 900 | round 2 test, first 600 | 96.5% | 1.9% | 1.7% | 89.2% |
| round 2, step 500 | round 2 test (3,000) | 95.5% | 2.7% | 1.8% | 95.5% |
| round 2, step 500 | round 3 test (3,000) | 95.5% | 2.9% | 1.6% | 93.5% |
| round 3 | round 3 val (2,000) | 99.4% | 0.6% | 0.1% | 97.7% |
| round 3 | round 3 test (3,000) | 98.0% | 1.2% | 0.8% | 95.7% |
| round 3, threshold 0.8 | round 3 val (2,000) | 98.9% | 0.07% | 1.0% | 98.6% |
| round 3, threshold 0.8 | round 3 test (3,000) | 97.1% | 0.14% | 2.8% | 97.1% |
| round 3, threshold 0.8 | round 3 test, steps that reach the model (1,632) | 94.6% | 0.36% | 5.0% | 98.3% |

The threshold is `LocalModelProvider`'s confidence check: the provider asks Ollama for the log probabilities of the reply, and a pick whose tokens have a probability under 0.8 is declined like a null pick. 0.8 was chosen on the validation split, where wrong picks fall from 0.6% to 0.07% for 0.5 points of correct picks. It is the default, and `MENDWRIGHT_LOCAL_MIN_CONFIDENCE` or `min_confidence=` changes it (0 turns it off).

On the round 3 test split without the threshold, by part:

- held-out page families: search results 96.9% correct (2.7% wrong), 92.6% of negatives declined; sign-up wizard 96.2% (0.7% wrong), 94.5%; blog article 99.2% (0.8% wrong), 96.1%
- flow: 95.3% of steps that moved to another page declined; 94.3% correct after a route rename, 96.6% for a control of the site chrome met on another page
- `intent=`: 98.0% correct with it, 97.9% for a key met for the first time with only `intent=` and `role=`

On the [benchmark](../docs/benchmark.md) (47 cases, StandInPage, with the fingerprint tier in front):

| Provider | Correct | Wrong | Missed | Negatives declined | Wrong on negatives |
|---|---|---|---|---|---|
| fingerprint tier alone | 21/35 | 0 | 14 | 12/12 | 0 |
| similarity baseline with the fingerprint tier | 28/35 | 1 | 6 | 10/12 | 2 |
| local model, threshold 0.8 (default) | 30/35 | 0 | 5 | 11/12 | 1 |
| local model, no threshold | 31/35 | 1 | 3 | 11/12 | 1 |
| round 2 model, no threshold | 28/35 | 3 | 4 | 11/12 | 1 |

The run in headless Chromium gave the same counts as StandInPage. The page guard declines the four flow cases before any model call; with the threshold, the one wrong heal left is on `neg-ambiguous-invite-accept`, two invitations that nothing in the step tells apart.

Speed and size, on the laptop above with Ollama 0.40.3:

- the model file (q4_K_M GGUF) is 986 MB; Ollama reports 1.2 GB loaded with a 4,096-token context
- GPU: 0.26 s per answer on average (0.24 s median, 0.44 s at the 95th percentile) over the 3,000 test steps, including the HTTP round trip; a whole heal in the benchmark took 0.20 s of provider time on average
- CPU only (`num_gpu` 0, on the i7-13620H): 1.5 s per answer on average (1.1 s median, 5.0 s at the 95th percentile) over 200 test steps

## Limitations

- The training pages are synthetic. They cover common layouts and redesigns, not everything real sites do.
- The candidate list holds 40 elements. On a page with hundreds of links, the right one can be left out when it shares no words with the step.
- Values that capture redacts, such as the `value` of a submit input, are not in the candidate list either.
- Seven languages. Others fall back on ids, names and structure.
