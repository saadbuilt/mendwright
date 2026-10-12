"""Merge the LoRA adapter into the base model and create the Ollama model.

    python training/export.py --adapter RUN/adapter --out EXPORT_DIR [--name mendwright-selector]
                              [--quantize q4_K_M] [--gguf]

1. Loads the base model in fp16 on the CPU, applies the adapter, merges it and saves
   EXPORT_DIR/merged (safetensors + tokenizer).
2. Writes EXPORT_DIR/Modelfile from Modelfile.template, with the system prompt from
   mendwright.candidates.SYSTEM_PROMPT.
3. Runs `ollama create <name> -f Modelfile --quantize <q>` on the merged safetensors. With
   --gguf (or when that fails) it converts the merged model to a GGUF f16 file with
   llama.cpp's convert_hf_to_gguf.py first and creates the model from that.

OLLAMA (the ollama executable) and LLAMA_CPP (the llama.cpp checkout) can be set in the
environment.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from mendwright.candidates import SYSTEM_PROMPT  # noqa: E402

DEFAULT_BASE = os.environ.get('MW_BASE', r'C:\Users\Omen\mendwright-train\base\Qwen2.5-1.5B-Instruct')


def ollama_exe():
    exe = os.environ.get('OLLAMA') or shutil.which('ollama')
    if not exe:
        guess = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Ollama' / 'ollama.exe'
        exe = str(guess) if guess.is_file() else 'ollama'
    return exe


def merge(base, adapter, out):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float16, device_map={'': 'cpu'})
    model = PeftModel.from_pretrained(model, adapter)
    model = model.merge_and_unload()
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out, safe_serialization=True)
    AutoTokenizer.from_pretrained(base).save_pretrained(out)
    for name in ('generation_config.json', 'LICENSE'):
        src = Path(base) / name
        if src.is_file():
            shutil.copy(src, out / name)
    print('merged model saved to', out, flush=True)


def modelfile(source, path):
    text = (HERE / 'Modelfile.template').read_text(encoding='utf-8')
    text = text.replace('{FROM}', str(source)).replace('{SYSTEM}', SYSTEM_PROMPT)
    path.write_text(text, encoding='utf-8')
    return path


def create(name, mfile, quantize):
    cmd = [ollama_exe(), 'create', name, '-f', str(mfile)]
    if quantize:
        cmd += ['--quantize', quantize]
    print(' '.join(cmd), flush=True)
    return subprocess.run(cmd).returncode == 0


def to_gguf(merged, out_file):
    llama = Path(os.environ.get('LLAMA_CPP', r'C:\Users\Omen\mendwright-train\llama.cpp'))
    cmd = [sys.executable, str(llama / 'convert_hf_to_gguf.py'), str(merged), '--outfile', str(out_file),
           '--outtype', 'f16']
    print(' '.join(cmd), flush=True)
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit('GGUF conversion failed')
    return out_file


def main(argv=None):
    parser = argparse.ArgumentParser(description='Merge the adapter and create the Ollama model.')
    parser.add_argument('--adapter', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--base', default=DEFAULT_BASE)
    parser.add_argument('--name', default='mendwright-selector')
    parser.add_argument('--quantize', default='q4_K_M', help="'' for none (f16)")
    parser.add_argument('--gguf', action='store_true', help='go through a GGUF f16 file')
    parser.add_argument('--skip-merge', action='store_true')
    args = parser.parse_args(argv)
    out = Path(args.out)
    merged = out / 'merged'
    if not args.skip_merge:
        merge(args.base, args.adapter, merged)
    if not args.gguf:
        mfile = modelfile(merged, out / 'Modelfile')
        if create(args.name, mfile, args.quantize):
            print('created', args.name, 'from safetensors', flush=True)
            return 0
        print('ollama create from safetensors failed; converting to GGUF', flush=True)
    gguf = out / (args.name + '-f16.gguf')
    if not gguf.is_file():
        to_gguf(merged, gguf)
    mfile = modelfile(gguf, out / 'Modelfile')
    if not create(args.name, mfile, args.quantize):
        raise SystemExit('ollama create failed')
    print('created', args.name, 'from', gguf, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
