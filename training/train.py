"""QLoRA fine-tuning of Qwen2.5-1.5B-Instruct on the dataset build_dataset.py writes.

The base model is loaded in 4-bit NF4 with bf16 compute, LoRA adapters go on every linear
layer, and the loss is only on the answer: the prompt is the base model's chat template
with the system and user messages and the generation prompt, the target is the answer
followed by <|im_end|>. Only the logits of the answer positions are computed
(logits_to_keep), which keeps a 6 GB GPU well inside its memory. Batch size 1 with
gradient accumulation, cosine learning rate with warmup, gradient checkpointing.

    python training/train.py --data DATA_DIR --out RUN_DIR [--epochs 1] [--limit N]

Writes RUN_DIR/adapter (the LoRA weights), a checkpoint every --save-every optimizer steps
(adapter, optimizer state and position) and a log. Progress lines go to stdout and
RUN_DIR/train.log; run it in the background and read the log. --resume goes on from the
last checkpoint after an interruption (the example order is fixed by --seed).
"""

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

import torch
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from mendwright.candidates import parse_answer  # noqa: E402

TARGETS = ['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj']


def log(fh, text):
    line = time.strftime('%H:%M:%S') + ' ' + text
    print(line, flush=True)
    if fh is not None:
        fh.write(line + '\n')
        fh.flush()


def load_jsonl(path, limit=None, seed=0):
    rows = [json.loads(line) for line in open(path, encoding='utf-8')]
    if limit and len(rows) > limit:
        rnd = random.Random(seed)
        rnd.shuffle(rows)
        rows = rows[:limit]
    return rows


def prompt_ids(tok, row):
    messages = [{'role': 'system', 'content': row['system']}, {'role': 'user', 'content': row['user']}]
    text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return tok(text, add_special_tokens=False)['input_ids']


def encode(tok, row, end_id):
    p = prompt_ids(tok, row)
    a = tok(row['answer'], add_special_tokens=False)['input_ids'] + [end_id]
    return p, a


def load_model(base, adapter=None, train=False, rank=16, alpha=32, dropout=0.05):
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForCausalLM, BitsAndBytesConfig
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                               bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(base, quantization_config=quant, dtype=torch.bfloat16,
                                                 device_map={'': 0}, attn_implementation='sdpa')
    model.config.use_cache = not train
    if train:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
        model.enable_input_require_grads()
    if adapter:
        model = PeftModel.from_pretrained(model, adapter, is_trainable=train)
    elif train:
        cfg = LoraConfig(r=rank, lora_alpha=alpha, lora_dropout=dropout, target_modules=TARGETS,
                         bias='none', task_type='CAUSAL_LM')
        model = get_peft_model(model, cfg)
    if train:
        model.train()
    else:
        model.eval()
    return model


@torch.no_grad()
def quick_eval(model, tok, rows, max_new=16, batch=8):
    """Greedy decode on rows; returns (accuracy, json_ok, n). Left padding, batched."""
    model.eval()
    model.config.use_cache = True
    tok.padding_side = 'left'
    correct = ok = 0
    order = sorted(range(len(rows)), key=lambda i: len(rows[i]['user']))
    for start in range(0, len(order), batch):
        idx = order[start:start + batch]
        texts = [tok.apply_chat_template([{'role': 'system', 'content': rows[i]['system']},
                                          {'role': 'user', 'content': rows[i]['user']}],
                                         tokenize=False, add_generation_prompt=True) for i in idx]
        enc = tok(texts, return_tensors='pt', padding=True, add_special_tokens=False).to('cuda')
        out = model.generate(**enc, max_new_tokens=max_new, do_sample=False, temperature=None,
                             top_p=None, top_k=None, pad_token_id=tok.pad_token_id)
        for j, i in enumerate(idx):
            text = tok.decode(out[j][enc['input_ids'].shape[1]:], skip_special_tokens=True)
            ans = parse_answer(text)
            if ans is not None:
                ok += 1
                if ans['pick'] == rows[i]['label']:
                    correct += 1
    model.config.use_cache = False
    model.train()
    return correct / max(len(rows), 1), ok / max(len(rows), 1), len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description='QLoRA fine-tune for the mendwright local selector model.')
    parser.add_argument('--data', required=True, help='folder with train.jsonl and val.jsonl')
    parser.add_argument('--out', required=True, help='run folder (adapter, checkpoints, log)')
    parser.add_argument('--base', default=os.environ.get('MW_BASE', r'C:\Users\Omen\mendwright-train\base\Qwen2.5-1.5B-Instruct'))
    parser.add_argument('--epochs', type=float, default=1.0)
    parser.add_argument('--limit', type=int, default=0, help='use at most N training examples')
    parser.add_argument('--lr', type=float, default=2e-4)
    parser.add_argument('--rank', type=int, default=16)
    parser.add_argument('--alpha', type=int, default=32)
    parser.add_argument('--accum', type=int, default=16)
    parser.add_argument('--warmup', type=float, default=0.03)
    parser.add_argument('--max-len', type=int, default=2048)
    parser.add_argument('--eval-every', type=int, default=400, help='optimizer steps between quick evals')
    parser.add_argument('--eval-n', type=int, default=240)
    parser.add_argument('--save-every', type=int, default=400)
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--mem-fraction', type=float, default=0.8)
    parser.add_argument('--resume', action='store_true', help='go on from RUN_DIR/checkpoint')
    parser.add_argument('--init', help='start from this LoRA adapter (a later round) instead of a new one')
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fh = open(out / 'train.log', 'a', encoding='utf-8')
    log(fh, 'args ' + json.dumps(vars(args)))
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    torch.cuda.set_per_process_memory_fraction(args.mem_fraction)

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.base)
    end_id = tok.convert_tokens_to_ids('<|im_end|>')
    train_rows = load_jsonl(Path(args.data) / 'train.jsonl', args.limit, args.seed)
    val_rows = load_jsonl(Path(args.data) / 'val.jsonl', args.eval_n, args.seed)
    data = []
    dropped = 0
    for row in train_rows:
        p, a = encode(tok, row, end_id)
        if len(p) + len(a) > args.max_len:
            dropped += 1
            continue
        data.append((p, a))
    lengths = sorted(len(p) + len(a) for p, a in data)
    log(fh, 'train examples %d (dropped %d over %d tokens); tokens median %d p99 %d max %d' % (
        len(data), dropped, args.max_len, lengths[len(lengths) // 2], lengths[int(len(lengths) * 0.99)], lengths[-1]))

    ckpt = out / 'checkpoint'
    state_file = ckpt / 'trainer_state.json'
    resume = None
    if args.resume and state_file.is_file():
        resume = json.loads(state_file.read_text(encoding='utf-8'))
        log(fh, 'resuming from %s at step %d (example %d)' % (ckpt, resume['step'], resume['micro']))
    start = str(ckpt) if resume else (args.init or None)
    if start and not resume:
        log(fh, 'starting from adapter ' + start)
    model = load_model(args.base, adapter=start, train=True, rank=args.rank, alpha=args.alpha)
    params = [p for p in model.parameters() if p.requires_grad]
    log(fh, 'trainable params %.1fM' % (sum(p.numel() for p in params) / 1e6))
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0, betas=(0.9, 0.999))
    if resume and (ckpt / 'optimizer.pt').is_file():
        opt.load_state_dict(torch.load(ckpt / 'optimizer.pt', map_location='cuda', weights_only=True))
    total_micro = int(len(data) * args.epochs)
    total_steps = max(1, total_micro // args.accum)
    warm = max(1, int(total_steps * args.warmup))

    def lr_at(step):
        if step < warm:
            return args.lr * (step + 1) / warm
        progress = (step - warm) / max(1, total_steps - warm)
        return args.lr * (0.05 + 0.95 * 0.5 * (1 + math.cos(math.pi * min(1.0, progress))))

    order = []
    rnd = random.Random(args.seed)
    while len(order) < total_micro:
        epoch = list(range(len(data)))
        rnd.shuffle(epoch)
        order.extend(epoch)
    order = order[:total_micro]
    log(fh, 'optimizer steps %d (accum %d), warmup %d' % (total_steps, args.accum, warm))

    step = resume['step'] if resume else 0
    micro = resume['micro'] if resume else 0
    if micro:
        order = order[micro:]
    loss_sum = 0.0
    tok_count = 0
    started = time.time()
    window = time.time()
    opt.zero_grad(set_to_none=True)
    for i in order:
        p, a = data[i]
        ids = torch.tensor([p + a], device='cuda')
        k = len(a)
        out_ = model(input_ids=ids, logits_to_keep=k + 1)
        logits = out_.logits[:, :-1, :].float()
        target = ids[:, -k:]
        loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)), target.reshape(-1))
        (loss / args.accum).backward()
        loss_sum += loss.item()
        tok_count += ids.shape[1]
        micro += 1
        if micro % args.accum == 0:
            for group in opt.param_groups:
                group['lr'] = lr_at(step)
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            opt.step()
            opt.zero_grad(set_to_none=True)
            step += 1
            if step % 10 == 0 or step == total_steps:
                spent = time.time() - started
                done = micro - (resume['micro'] if resume else 0)
                eta = spent / max(done, 1) * (total_micro - micro)
                log(fh, 'step %d/%d loss %.4f lr %.2e %.2fs/ex %.0f tok/s elapsed %.1fm eta %.1fm mem %.2fG' % (
                    step, total_steps, loss_sum / (10 * args.accum), lr_at(step), (time.time() - window) / (10 * args.accum),
                    tok_count / (time.time() - window), spent / 60, eta / 60, torch.cuda.max_memory_allocated() / 1e9))
                loss_sum = 0.0
                tok_count = 0
                window = time.time()
            if args.eval_every and step % args.eval_every == 0:
                acc, ok, n = quick_eval(model, tok, val_rows)
                log(fh, 'eval step %d: val pick accuracy %.4f json %.4f (n=%d)' % (step, acc, ok, n))
            if args.save_every and step % args.save_every == 0:
                # Adapter, optimizer and position, so --resume can go on from here.
                model.save_pretrained(ckpt)
                torch.save(opt.state_dict(), ckpt / 'optimizer.pt')
                state_file.write_text(json.dumps({'step': step, 'micro': micro}), encoding='utf-8')
                log(fh, 'saved checkpoint at step %d' % step)
    model.save_pretrained(out / 'adapter')
    tok.save_pretrained(out / 'adapter')
    acc, ok, n = quick_eval(model, tok, val_rows)
    log(fh, 'final: val pick accuracy %.4f json %.4f (n=%d); %.1f minutes' % (acc, ok, n, (time.time() - started) / 60))
    log(fh, 'DONE')
    return 0


if __name__ == '__main__':
    sys.exit(main())
