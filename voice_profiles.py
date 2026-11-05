"""Trusted, checksum-verified voice references sharing one synthesis model."""
from dataclasses import dataclass
import hashlib,json,math,re,wave
from pathlib import Path
import numpy as np


MODEL_WEIGHTS = {
    'int8': ('encoder.int8.onnx', 'decoder.int8.onnx'),
    'fp32': ('fp32/encoder.onnx', 'fp32/decoder.onnx'),
    'full': ('full/text_encoder.onnx', 'full/fm_decoder.onnx'),
}

@dataclass(frozen=True)
class VoiceProfile:
    id: str
    name: str
    language: str
    description: str
    text: str
    samples: np.ndarray
    sha256: str
    steps: int = 4
    guidance_scale: float = 1.0
    output_lufs: float | None = None
    model_variant: str = 'int8'
    speed: float = 1.0
    prosody_mode: str = 'default'
    greeting_steps: int | None = None
    high_shelf_db: float = 0.0

    @property
    def fingerprint(self):
        # Transcript and rendering settings must invalidate cached PCM too.
        settings=[self.sha256,self.text,self.steps,self.guidance_scale,self.output_lufs,self.model_variant,self.speed,self.prosody_mode,self.greeting_steps]
        if self.high_shelf_db:
            settings.append({'high_shelf_db':self.high_shelf_db})
        return hashlib.sha256(json.dumps(settings,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def load_profiles(config):
    root=Path(config['model_dir']).resolve()
    catalog_path=config.get('catalog_path')
    if catalog_path:
        path=Path(catalog_path)
        if path.stat().st_size>131072:raise ValueError('Voice catalog too large')
        catalog=json.loads(path.read_text(encoding='utf-8'))
        default=catalog['default_voice_id'];entries=catalog['voices']
    else:
        path=Path(config.get('profile_path',root/'profile.json'))
        entry=json.loads(path.read_text(encoding='utf-8'))
        reference=(path.parent/entry.get('reference_file','reference.wav')).resolve()
        entry.update(id='xiaoya-a',name='温柔桃子（升级版）',language='zh',description='温柔自然',reference_file=str(reference))
        default='xiaoya-a';entries=[entry]
    if not isinstance(entries,list)or not 1<=len(entries)<=32:raise ValueError('Invalid voice catalog')
    result={}
    for entry in entries:
        identity=entry['id']
        if not isinstance(identity,str)or not re.fullmatch(r'[a-z0-9_-]{1,48}',identity)or identity in result:raise ValueError('Invalid voice ID')
        if entry.get('language')not in('zh','en')or not isinstance(entry.get('name'),str)or not 1<=len(entry['name'])<=48:raise ValueError('Invalid voice metadata')
        text=entry['reference_text']
        if not isinstance(text,str)or not 1<=len(text)<=500:raise ValueError('Invalid reference transcript')
        steps=entry.get('steps',4);guidance=entry.get('guidance_scale',1.0);lufs=entry.get('output_lufs')
        variant=entry.get('model_variant','int8')
        speed=entry.get('speed',1.0);mode=entry.get('prosody_mode','default')
        if type(speed)not in(int,float)or not math.isfinite(speed)or not .85<=speed<=1.15:raise ValueError('Invalid synthesis speed')
        if mode not in('default','chat'):raise ValueError('Invalid prosody mode')
        if not isinstance(variant,str) or variant not in MODEL_WEIGHTS:raise ValueError('Invalid local model variant')
        if type(steps)is not int or steps not in(4,8,12,16):raise ValueError('Invalid synthesis steps')
        greeting_steps=entry.get('greeting_steps')
        if greeting_steps is not None and(type(greeting_steps)is not int or greeting_steps not in(4,8,12,16)or greeting_steps<steps):raise ValueError('Invalid greeting steps')
        if type(guidance)not in(int,float)or guidance not in(1.0,3.0):raise ValueError('Invalid guidance scale')
        if lufs is not None and(type(lufs)not in(int,float)or not math.isfinite(lufs)or not -24<=lufs<=-12):raise ValueError('Invalid loudness target')
        high_shelf_db=entry.get('high_shelf_db',0.0)
        if type(high_shelf_db) not in (int,float) or not -4<=high_shelf_db<=0 or (high_shelf_db and lufs is None):
            raise ValueError('Invalid voice high shelf gain')
        reference=(root/entry['reference_file']).resolve()
        # Legacy external references are accepted only through the explicit config path.
        if catalog_path and not reference.is_relative_to(root):raise ValueError('Reference escapes model directory')
        if reference.stat().st_size>1500000:raise ValueError('Reference too large')
        digest=hashlib.sha256(reference.read_bytes()).hexdigest()
        if digest!=entry['reference_sha256']:raise ValueError('Reference checksum mismatch')
        with wave.open(str(reference),'rb')as audio:
            if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate())!=(1,2,24000):raise ValueError('Invalid reference format')
            if not 1<=audio.getnframes()/24000<=15:raise ValueError('Invalid reference duration')
            samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2').astype(np.float32)/32768
        if not np.any(samples):raise ValueError('Silent reference')
        result[identity]=VoiceProfile(identity,entry['name'],entry['language'],str(entry.get('description',''))[:100],text,samples,digest,steps,float(guidance),lufs,variant,float(speed),mode,greeting_steps,float(high_shelf_db))
    if default not in result:raise ValueError('Default voice missing')
    return default,result
