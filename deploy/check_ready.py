"""只读等待本机服务完成预热，并检查使用了目标资源配置。"""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8793/health')
    parser.add_argument('--timeout',type=float,default=150)
    parser.add_argument('--target',default='amd-4vcpu-12gb')
    args=parser.parse_args()
    parsed=urllib.parse.urlsplit(args.url)
    if parsed.scheme!='http'or parsed.hostname!='127.0.0.1'or parsed.path!='/health'or not 0<args.timeout<=180:
        parser.error('Use a loopback health URL and timeout of 1..180 seconds')
    deadline=time.monotonic()+args.timeout;last='not listening'
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen(args.url,timeout=2)as response:body=json.load(response)
            if body.get('ready')and body.get('runtime',{}).get('target')==args.target:
                print(json.dumps({'ready':True,'runtime':body['runtime']}));return 0
            last='not ready or different resource target'
        except (OSError,ValueError,urllib.error.URLError):last='not ready'
        time.sleep(.5)
    print(json.dumps({'ready':False,'reason':last}));return 1


if __name__=='__main__':raise SystemExit(main())
