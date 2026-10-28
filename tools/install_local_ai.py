"""Download pinned, checksum-verified local inference files. No credentials needed."""
from pathlib import Path
import hashlib
import json
import time
import zipfile
import requests

ROOT = Path(__file__).resolve().parents[1] / '.local-ai'
FILES = [
    ('https://github.com/ggml-org/llama.cpp/releases/download/b11809/llama-b11809-bin-win-cpu-x64.zip',
     'downloads/llama-b11809-bin-win-cpu-x64.zip', 'eabec16af2b221639d865d17f7e3ed654e53dfe5f26ff7823b1f7010e6c90a1f'),
    ('https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-Q4_K_M.gguf',
     'models/Qwen3.5-2B-Q4_K_M.gguf', 'aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223'),
    ('https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/mmproj-F16.gguf',
     'models/mmproj-F16.gguf', '7035e9cb8d7c6a9681d07eef9a364783e86ea4cd73faab2eabb4f43a101830c7'),
]

def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()

def download(url, relative, digest):
    dest = ROOT / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256(dest) == digest:
        print(f'VERIFIED {relative}', flush=True)
        return dest
    part = dest.with_suffix(dest.suffix + '.part')
    for attempt in range(4):
        try:
            offset = part.stat().st_size if part.exists() else 0
            headers = {'Range': f'bytes={offset}-'} if offset else {}
            with requests.get(url, headers=headers, stream=True, timeout=(20, 60)) as response:
                response.raise_for_status()
                append = offset and response.status_code == 206
                if not append:
                    offset = 0
                total = offset + int(response.headers.get('Content-Length') or 0)
                last = time.monotonic()
                with part.open('ab' if append else 'wb') as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        handle.write(chunk)
                        offset += len(chunk)
                        if time.monotonic() - last >= 15:
                            print(f'{relative}: {offset//1048576}/{total//1048576} MiB', flush=True)
                            last = time.monotonic()
            if sha256(part) != digest:
                part.unlink(missing_ok=True)
                raise RuntimeError(f'Checksum mismatch: {relative}')
            part.replace(dest)
            print(f'VERIFIED {relative}', flush=True)
            return dest
        except Exception as exc:
            print(f'Download attempt {attempt+1}: {type(exc).__name__}: {exc}', flush=True)
            if attempt == 3:
                raise
            time.sleep(2)

if __name__ == '__main__':
    for url, relative, digest in FILES:
        file = download(url, relative, digest)
        if file.suffix == '.zip':
            target = (ROOT / 'runtime').resolve()
            with zipfile.ZipFile(file) as archive:
                for name in archive.namelist():
                    if not (target / name).resolve().is_relative_to(target):
                        raise RuntimeError('Unsafe archive path')
                archive.extractall(target)
    (ROOT / 'manifest.json').write_text(json.dumps(FILES, indent=2), encoding='utf-8')
    print('Local AI files ready.', flush=True)
