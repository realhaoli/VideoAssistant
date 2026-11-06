"""Download the official multilingual Whisper small CTranslate2 weights locally."""
from pathlib import Path
import hashlib
import json
import os
import time
import urllib.request
from dotenv import load_dotenv

root = Path(__file__).resolve().parents[1]
load_dotenv(root / 'variables.env')
folder = Path(os.getenv('MEDIA_DATA_DIR', root / 'data')).expanduser().resolve() / 'models' / 'faster-whisper-small'
folder.mkdir(parents=True, exist_ok=True)
metadata = {'siblings': []}
for attempt in range(3):
    try:
        metadata = json.load(urllib.request.urlopen('https://huggingface.co/api/models/Systran/faster-whisper-small?blobs=true', timeout=40))
        break
    except Exception:
        if attempt == 2:
            print('Model metadata unavailable; trying official weight downloads directly.', flush=True)
        else:
            time.sleep(2)
files = {f['rfilename']: f for f in metadata['siblings']}
for name in ('config.json', 'tokenizer.json', 'vocabulary.txt', 'model.bin'):
    target = folder / name
    expected = files.get(name, {})
    if target.exists() and target.stat().st_size == expected.get('size', target.stat().st_size):
        print('Already available:', name, flush=True)
        continue
    partial = target.with_suffix(target.suffix + '.part')
    for attempt in range(4):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            request = urllib.request.Request('https://huggingface.co/Systran/faster-whisper-small/resolve/main/' + name, headers={'Range': f'bytes={offset}-'} if offset else {})
            with urllib.request.urlopen(request, timeout=90) as response:
                append = response.status == 206
                with partial.open('ab' if append else 'wb') as dest:
                    while block := response.read(1024 * 1024):
                        dest.write(block)
            if expected.get('size') and partial.stat().st_size != expected['size']:
                raise ValueError('Download size mismatch')
            expected_hash = expected.get('lfs', {}).get('sha256')
            if expected_hash:
                digest = hashlib.sha256()
                with partial.open('rb') as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                if digest.hexdigest() != expected_hash:
                    partial.unlink()
                    raise ValueError('Model SHA256 mismatch')
            partial.replace(target)
            print('Downloaded:', name, target.stat().st_size, 'bytes', flush=True)
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2)
print('Whisper small ready:', folder, flush=True)
