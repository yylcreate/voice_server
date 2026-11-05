"""Load the exact, privately deployed WAVs for the configured voice catalog."""
import hashlib
import io
import json
from pathlib import Path
import wave


def load_previews(config, voice_ids):
    manifest_path = config.get('preview_manifest')
    if not manifest_path:
        return {}
    root = Path(config['model_dir']).resolve()
    manifest = Path(manifest_path).resolve()
    if not manifest.is_relative_to(root) or manifest.stat().st_size > 32768:
        raise ValueError('Invalid preview manifest path or size')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    rows = data['voices']
    if (data.get('schema') != 1 or data.get('asset_kind') != 'fixed_preview_wav'
            or len(rows) != len(voice_ids) or {row['id'] for row in rows} != set(voice_ids)):
        raise ValueError('Preview manifest does not match voice catalog')
    previews = {}
    for row in rows:
        name = row['id'] + '.wav'
        path = (manifest.parent / name).resolve()
        if row['file'] != name or path.parent != manifest.parent or not 44 < path.stat().st_size <= 1500000:
            raise ValueError('Invalid preview file')
        audio = path.read_bytes()
        if len(audio) != row['bytes'] or hashlib.sha256(audio).hexdigest() != row['sha256']:
            raise ValueError('Preview checksum mismatch')
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            frames = wav.getnframes()
            if ((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000)
                    or not frames or len(wav.readframes(frames)) != frames * 2):
                raise ValueError('Invalid preview audio format')
        previews[row['id']] = audio
    return previews
