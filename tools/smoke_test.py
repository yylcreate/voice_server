"""用私有配置签名并验证实际PCM流；输出仅包含非敏感测量。--yyl"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
from pathlib import Path
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--base-url',default='http://127.0.0.1:8793')
    parser.add_argument('--user-id',type=int,default=90000001)
    parser.add_argument('--voice',default='xiaoya-a')
    parser.add_argument('--text',default='你好！我是小雅，你的精密制造智能工作伙伴。')
    args=parser.parse_args()
    base=args.base_url.rstrip('/');url=urllib.parse.urlsplit(base)
    if (url.scheme not in('http','https')or not url.hostname or url.path or url.query or url.fragment or
        url.username or url.password or (url.scheme=='http'and url.hostname not in('127.0.0.1','localhost'))or args.user_id<=0):
        parser.error('Use HTTPS (or local loopback HTTP), no URL path or embedded credentials, and a positive test user.')
    secret=json.loads(args.config.read_text(encoding='utf-8-sig'))['secret']
    if not isinstance(secret,str)or len(secret)<32:parser.error('Private signing configuration is missing.')
    path=f'/v1/stream?user_id={args.user_id}'
    body={'user_id':args.user_id,'request_id':uuid.uuid4().hex,'text':args.text,'voice_id':args.voice}
    raw=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()
    stamp=str(int(time.time()));nonce=uuid.uuid4().hex
    message='\n'.join(['POST',path,stamp,nonce,hashlib.sha256(raw).hexdigest()])
    headers={'Content-Type':'application/json','X-Hht-Timestamp':stamp,'X-Hht-Nonce':nonce,
             'X-Hht-Signature':hmac.new(secret.encode(),message.encode(),hashlib.sha256).hexdigest()}
    headers['User-Agent']='Xiaoya-Voice-Deployment-Probe/1.0'  # 明确测试客户端身份，兼容边缘通用脚本UA策略。--yyl
    request=urllib.request.Request(base+path,data=raw,headers=headers,method='POST')
    start=time.perf_counter();pending=bytearray();magic=False;metadata=None;complete=None;first_pcm=None
    pcm_bytes=0;digest=hashlib.sha256();progress=[]
    try:
        with urllib.request.urlopen(request,timeout=180)as response:
            header_ms=(time.perf_counter()-start)*1000
            while chunk:=response.read1(8192):
                pending.extend(chunk)
                if not magic:
                    if len(pending)<8:continue
                    if bytes(pending[:8])!=b'XYVOICE1':raise ValueError('Invalid speech protocol')
                    del pending[:8];magic=True
                while len(pending)>=5:
                    kind,size=struct.unpack('>BI',pending[:5])
                    if size>4*1024*1024:raise ValueError('Oversized voice frame')
                    if len(pending)<size+5:break
                    data=bytes(pending[5:5+size]);del pending[:5+size]
                    if kind==1:metadata=json.loads(data)
                    elif kind==2:progress.append(json.loads(data))
                    elif kind==3:
                        if first_pcm is None:first_pcm=(time.perf_counter()-start)*1000
                        pcm_bytes+=len(data);digest.update(data)
                    elif kind==4:complete=json.loads(data)
                    elif kind==5:raise ValueError('Voice service returned an error frame')
                    else:raise ValueError('Unknown voice frame')
    except urllib.error.HTTPError as error:
        raise SystemExit(f'Voice HTTP request failed: {error.code}')from None
    if (pending or not pcm_bytes or pcm_bytes%2 or metadata is None or not complete or
        complete.get('complete')is not True or metadata.get('voice_id')!=args.voice or
        metadata.get('rate')!=24000 or metadata.get('channels')!=1 or metadata.get('format')!='pcm_s16le'or
        complete.get('segments')!=metadata.get('total')or len(progress)!=metadata.get('total')):
        raise SystemExit('Incomplete or inconsistent PCM stream')
    print(json.dumps({'complete':True,'voice_id':args.voice,'header_ms':round(header_ms,3),
        'first_pcm_ms':round(first_pcm,3),'complete_ms':round((time.perf_counter()-start)*1000,3),
        'audio_seconds':pcm_bytes/48000,'segments':len(progress),'cached':[p['cached']for p in progress],
        'pcm_sha256':digest.hexdigest()},ensure_ascii=False))


if __name__=='__main__':main()
