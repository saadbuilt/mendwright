"""Merge the LoRA adapter into the base model and create the Ollama model.

    python training/export.py --adapter RUN/adapter --out EXPORT_DIR [--name mendwright-selector]
                              [--quantize q4_K_M]

1. Loads the base model in fp16 on the CPU, applies the adapter, merges it and saves
   EXPORT_DIR/merged (safetensors + tokenizer).
2. Writes EXPORT_DIR/Modelfile from Modelfile.template, with the system prompt from
   mendwright.candidates.SYSTEM_PROMPT.
3. Tries `ollama create <name> -f Modelfile --quantize <q>` on the merged safetensors.
   Ollama 0.40 quantizes safetensors only to its own int4/int8 formats, so for a llama.cpp
   type such as q4_K_M this fails, and the script goes on: llama.cpp's
   convert_hf_to_gguf.py writes a GGUF f16 file, llama-quantize turns it into the GGUF
   type asked for, and `ollama create` imports that file. Without llama-quantize, q8_0 is
   written by convert_hf_to_gguf.py itself.

Environment: OLLAMA (the ollama executable), LLAMA_CPP (the llama.cpp checkout) and
LLAMA_QUANTIZE (the llama-quantize executable; default: llama.cpp/build*/bin/). Building
llama-quantize on Windows: training/README.md.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from mendwright.candidates import SYSTEM_PROMPT  # noqa: E402

DEFAULT_BASE = os.environ.get('MW_BASE', r'C:\Users\Omen\mendwright-train\base\Qwen2.5-1.5B-Instruct')
LLAMA_CPP = Path(os.environ.get('LLAMA_CPP', r'C:\Users\Omen\mendwright-train\llama.cpp'))


def ollama_exe():
    exe = os.environ.get('OLLAMA') or shutil.which('ollama')
    if not exe:
        guess = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Ollama' / 'ollama.exe'
        exe = str(guess) if guess.is_file() else 'ollama'
    return exe


def quantize_exe():
    exe = os.environ.get('LLAMA_QUANTIZE')
    if exe and Path(exe).is_file():
        return exe
    for pattern in ('build*/bin/llama-quantize.exe', 'build*/bin/llama-quantize',
                    'build*/bin/Release/llama-quantize.exe'):
        hits = sorted(glob.glob(str(LLAMA_CPP / pattern)))
        if hits:
            return hits[0]
    return shutil.which('llama-quantize')


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


def create(name, mfile, quantize=None):
    cmd = [ollama_exe(), 'create', name, '-f', str(mfile)]
    if quantize:
        cmd += ['--quantize', quantize]
    print(' '.join(cmd), flush=True)
    return subprocess.run(cmd).returncode == 0


def run(cmd, env=None):
    print(' '.join(str(c) for c in cmd), flush=True)
    if subprocess.run([str(c) for c in cmd], env=env).returncode != 0:
        raise SystemExit('failed: ' + str(cmd[0]))


def to_gguf(merged, out_file, outtype='f16'):
    run([sys.executable, LLAMA_CPP / 'convert_hf_to_gguf.py', merged, '--outfile', out_file,
         '--outtype', outtype])
    return out_file


def main(argv=None):
    parser = argparse.ArgumentParser(description='Merge the adapter and create the Ollama model.')
    parser.add_argument('--adapter', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--base', default=DEFAULT_BASE)
    parser.add_argument('--name', default='mendwright-selector')
    parser.add_argument('--quantize', default='q4_K_M', help="a llama.cpp type (q4_K_M, q8_0), or '' for f16")
    parser.add_argument('--skip-merge', action='store_true')
    args = parser.parse_args(argv)
    out = Path(args.out)
    merged = out / 'merged'
    if not args.skip_merge:
        merge(args.base, args.adapter, merged)
    if create(args.name, modelfile(merged, out / 'Modelfile'), args.quantize or None):
        print('created', args.name, 'from the merged safetensors', flush=True)
        return 0
    print('ollama could not create it from safetensors; going through GGUF', flush=True)
    qtype = (args.quantize or 'f16').upper() if args.quantize else 'F16'
    f16 = out / (args.name.replace(':', '-') + '-f16.gguf')
    if not f16.is_file():
        to_gguf(merged, f16)
    target = f16
    if qtype != 'F16':
        target = out / (args.name.replace(':', '-') + '-' + qtype.lower() + '.gguf')
        exe = quantize_exe()
        if exe:
            env = dict(os.environ)
            mingw = Path(r'C:\msys64\ucrt64\bin')   # runtime DLLs of a MinGW build
            if mingw.is_dir():
                env['PATH'] = str(mingw) + os.pathsep + env.get('PATH', '')
            run([exe, f16, target, qtype, str(os.cpu_count() or 4)], env=env)
        elif qtype == 'Q8_0':
            to_gguf(merged, target, 'q8_0')
        else:
            raise SystemExit('llama-quantize not found: build it (training/README.md) or use --quantize q8_0')
    if not create(args.name, modelfile(target, out / 'Modelfile')):
        raise SystemExit('ollama create failed')
    print('created', args.name, 'from', target, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
