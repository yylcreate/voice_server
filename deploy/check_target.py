"""只读核验目标资源配置与现有参考；输出不包含服务密钥或转写。"""
import argparse
import ctypes
import json
import os
import platform
import shutil
import sys
from pathlib import Path

SERVICE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SERVICE))
from runtime_profiles import TARGET,resource_config,resource_info
from voice_profiles import MODEL_WEIGHTS, load_profiles
from voice_previews import load_previews


def memory_mib():
    if sys.platform=='win32':
        class Memory(ctypes.Structure):
            _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(name,ctypes.c_ulonglong)for name in ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]
        value=Memory();value.length=ctypes.sizeof(value)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):return round(value.total/1024**2)
    path=Path('/proc/meminfo')
    if path.is_file():return round(int(next(line.split()[1]for line in path.read_text().splitlines()if line.startswith('MemTotal:')))/1024)
    return None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--require-target-hardware',action='store_true')
    args=parser.parse_args()
    config=resource_config(json.loads(args.config.read_text(encoding='utf-8-sig')),TARGET)
    if len(config.get('secret',''))<32:raise ValueError('Configure the existing backend service signing secret privately')
    if not shutil.which(config.get('ffmpeg','ffmpeg')):raise ValueError('Configured local FFmpeg is unavailable')
    default,voices=load_profiles(config)
    previews=load_previews(config,voices)
    model=Path(config['model_dir'])
    required=['tokens.txt','lexicon.txt','vocos_24khz.onnx','phone.fst','date.fst','number.fst']
    for variant in {v.model_variant for v in voices.values()}:
        required.extend(MODEL_WEIGHTS[variant])
    if any(not(model/name).is_file()for name in required):raise ValueError('Required local voice model files are missing')
    total=memory_mib();cpu=os.cpu_count()
    match=platform.system()=='Linux'and platform.machine().lower()in ('x86_64','amd64')and cpu==4 and total is not None and total>=11000
    result={'configuration_valid':True,'runtime':resource_info(config),'voice_count':len(voices),'preview_count':len(previews),'default_voice':default,
            'references_checksum_verified':True,'hardware':{'system':platform.system(),'machine':platform.machine(),
            'visible_logical_cpus':cpu,'memory_mib':total},'matches_target_baseline':match,'server_capacity_measured':False}
    print(json.dumps(result,ensure_ascii=False))
    return 0 if match or not args.require_target_hardware else 2


if __name__=='__main__':raise SystemExit(main())
