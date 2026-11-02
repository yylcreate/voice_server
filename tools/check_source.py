"""拒绝将运行时私有资产或认证材料加入本源码仓库。--yyl"""
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
PRIVATE_PARTS={'.venv','.models','.runtime','private','secrets'}
PRIVATE_SUFFIXES={'.wav','.mp3','.m4a','.pcm','.onnx','.npy','.npz','.pt','.pth','.safetensors',
                  '.zip','.tar','.gz','.tgz','.key','.pem','.p12','.pfx'}
PATTERNS=[re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
          re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}'),
          re.compile(rb'github_pat_[A-Za-z0-9_]{50,}')]


def main():
    names=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    failures=[];count=0
    for name in filter(None,names):
        path=Path(name);count+=1
        if PRIVATE_PARTS.intersection(path.parts)or path.suffix.lower()in PRIVATE_SUFFIXES:
            failures.append(name+': runtime asset or authentication file');continue
        if path.name=='config.json'or(path.name.startswith('.env')and path.name!='.env.example'):
            failures.append(name+': private runtime configuration');continue
        data=subprocess.check_output(['git','show',':'+name],cwd=ROOT)
        if len(data)>1024*1024:failures.append(name+': unexpectedly large source file')
        if any(pattern.search(data)for pattern in PATTERNS):failures.append(name+': authentication material pattern')
    if failures:raise SystemExit('\n'.join(failures))
    print(f'Checked {count} tracked source files; runtime assets and authentication material excluded.')


if __name__=='__main__':main()
