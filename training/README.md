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
- GPU: NVIDIA GeForce RTX 4050 Laptop, 6 GB (training used about 2.4 GB of it)
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
python training/build_dataset.py --out ../data/r1 --train 34000 --val 2000 --test 3000

# 2. Training: QLoRA, about 6 hours on the laptop above (resume with --resume)
../train-venv/Scripts/python training/train.py --data ../data/r1 --out ../runs/r1

# 3. Evaluation with greedy decoding
../train-venv/Scripts/python training/evaluate.py --data ../data/r1 --split test --adapter ../runs/r1/adapter

# 4. Merge, convert, quantize and create the Ollama model
../train-venv/Scripts/python training/export.py --adapter ../runs/r1/adapter --out ../export/r1

# 5. The shipped model through Ollama, and the benchmark
python training/evaluate.py --data ../data/r1 --split test --ollama mendwright-selector
PYTHONPATH=. python benchmarks/run.py --provider local --model mendwright-selector
```

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

DATASET_TABLE

## Training

QLoRA on Qwen2.5-1.5B-Instruct: 4-bit NF4 with double quantization, bf16 compute, LoRA rank 16 and alpha 32 with dropout 0.05 on every linear layer (18.5M trainable parameters), the base model's chat template, and the loss on the answer and its `<|im_end|>` only. Batch 1 with 16 steps of gradient accumulation, AdamW, a learning rate of 2e-4 with 3% warmup and a cosine decay, gradient checkpointing, one epoch. Only the logits of the answer positions are computed, which keeps the run at about 2.4 GB of GPU memory. Prompts are 152 to 2,032 tokens with a median of 315, so the 2,048-token limit cuts nothing.

ROUNDS_TABLE

## Results

RESULTS_TABLE

## Limitations

- The training pages are synthetic. They cover common layouts and redesigns, not everything real sites do.
- The candidate list holds 40 elements. On a page with hundreds of links, the right one can be left out when it shares no words with the step.
- Values that capture redacts, such as the `value` of a submit input, are not in the candidate list either.
- Seven languages. Others fall back on ids, names and structure.
