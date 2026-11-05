"""Install the dedicated voice virtual host without restarting models or editing the AI site. --yyl"""
from __future__ import annotations
import argparse,datetime,fcntl,hashlib,json,os,pathlib,re,socket,ssl,subprocess,tempfile,time,urllib.error,urllib.request

ROOT=pathlib.Path(__file__).resolve().parents[1]
SITE=pathlib.Path('/etc/nginx/sites-available/xiaoya-voice.conf')
ENABLED=pathlib.Path('/etc/nginx/sites-enabled/xiaoya-voice.conf')
SNIPPET=pathlib.Path('/etc/nginx/snippets/xiaoya-voice.conf')
CERT=pathlib.Path('/etc/nginx/ssl/aichat.6xmu.com.pem')
HOST='voice.6xmu.com'

def command(*args):return subprocess.check_output(args,text=True).strip()
def digest(data):return hashlib.sha256(data).hexdigest()
def inspect(name):return json.loads(command('docker','inspect',name))[0]
def health(url,host=None):
    request=urllib.request.Request(url,headers={'User-Agent':'Xiaoya-Voice-Deployment-Probe/1.0',**({'Host':host}if host else {})})
    with urllib.request.urlopen(request,timeout=5)as response:return json.load(response)
def signed_routes_reachable(base,host=None):
    headers={'User-Agent':'Xiaoya-Voice-Deployment-Probe/1.0',**({'Host':host}if host else {})}
    for path in ('/v1/preview','/v1/transcribe'):
        request=urllib.request.Request(base+path,data=b'{}',headers=headers)
        try:
            with urllib.request.urlopen(request,timeout=5):
                raise ValueError(path+' accepted an unsigned request')
        except urllib.error.HTTPError as error:
            with error:
                assert error.code==403 and json.load(error)=={'error':'invalid service signature'}, (path,error.code)
def replace(path,data,mode=0o644):
    handle,name=tempfile.mkstemp(prefix='.xiaoya-voice-domain-',dir=path.parent)
    try:
        with os.fdopen(handle,'wb')as output:output.write(data)
        os.chmod(name,mode);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
def other_sites():
    return {entry.name:{'target':str(entry.resolve()),'sha256':digest(entry.read_bytes())}
            for entry in pathlib.Path('/etc/nginx/sites-enabled').iterdir()if entry.name!=ENABLED.name and entry.is_file()}
def origin_tls():
    # 既有Origin证书作为此本机探针的显式信任锚，校验新SNI/主机名；不关闭校验或改变系统信任。--yyl
    context=ssl.create_default_context(cafile=str(CERT));context.verify_flags|=ssl.VERIFY_X509_PARTIAL_CHAIN
    with socket.create_connection(('127.0.0.1',443),timeout=5)as raw,context.wrap_socket(raw,server_hostname=HOST)as tls:
        tls.sendall(('GET /health HTTP/1.1\r\nHost: '+HOST+'\r\nConnection: close\r\n\r\n').encode())
        assert tls.recv(4096).startswith(b'HTTP/1.1 200'),'Origin TLS virtual host did not serve health'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--commit',required=True);args=parser.parse_args()
    assert os.geteuid()==0 and re.fullmatch('[a-f0-9]{40}',args.commit)
    assert command('git','-C',str(ROOT),'rev-parse','HEAD')==args.commit
    assert not command('git','-C',str(ROOT),'status','--porcelain')
    source=ROOT/'deploy/nginx.voice-domain.conf';data=source.read_bytes()
    assert SNIPPET.read_bytes()==(ROOT/'deploy/nginx.voice.conf').read_bytes()
    assert data.count(b'server_name voice.6xmu.com;')==2
    subprocess.run(['openssl','x509','-in',str(CERT),'-noout','-checkhost',HOST],check=True)
    subprocess.run(['openssl','x509','-in',str(CERT),'-noout','-checkend','86400'],check=True)
    with open('/run/lock/yilian-codex-deploy.lock','a')as shared,open('/run/lock/xiaoya-voice-deploy.lock','a')as voice_lock:
        fcntl.flock(shared,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(voice_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert not SITE.is_symlink(),'Dedicated available site must be a regular file'
        original=SITE.read_bytes()if SITE.exists()else None
        if original is not None:assert b'server_name voice.6xmu.com;'in original and b'server_name aichat.6xmu.com;'not in original
        had_enabled=ENABLED.is_symlink();assert not ENABLED.exists()or had_enabled
        if had_enabled:assert ENABLED.resolve()==SITE
        before_sites=other_sites();before_voice=inspect('xiaoya-voice-voice-1');before_ai=inspect('yilian-aim-codex-api')
        runtime_commit=before_voice['Config']['Labels']['org.opencontainers.image.revision']
        runtime_files=['server.py','voice_engine.py','voice_profiles.py','voice_loudness.py','voice_text.py','voice_asr.py','voice_previews.py','runtime_profiles.py','requirements.txt','Dockerfile','compose.yaml']
        assert not command('git','-C',str(ROOT),'diff','--name-only',runtime_commit,args.commit,'--',*runtime_files),'Runtime changed; use the model release process'
        assert health('http://127.0.0.1:8793/health')['ready']
        receipt=pathlib.Path('/var/lib/xiaoya-voice/domain-releases')/datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        receipt.mkdir(parents=True,mode=0o700,exist_ok=False)
        if original is not None:(receipt/'voice-site.before').write_bytes(original)
        (receipt/'other-sites.before.json').write_text(json.dumps(before_sites,indent=2)+'\n')
        try:
            replace(SITE,data)
            if not had_enabled:ENABLED.symlink_to(SITE)
            subprocess.run(['nginx','-t'],check=True)
            subprocess.run(['systemctl','reload','nginx'],check=True)
            last_error=None
            for attempt in range(10):
                try:
                    local=health('http://127.0.0.1/health',HOST)
                    public=health('https://'+HOST+'/health')
                    assert local['ready'] and public['ready']
                    origin_tls()
                    signed_routes_reachable('http://127.0.0.1',HOST)
                    signed_routes_reachable('https://'+HOST)
                    break
                except Exception as error:last_error=error;time.sleep(.3)
            else:raise RuntimeError('Dedicated domain readiness did not converge')from last_error
            assert other_sites()==before_sites
            assert inspect('xiaoya-voice-voice-1')['Id']==before_voice['Id'] and inspect('yilian-aim-codex-api')['Id']==before_ai['Id']
            ai=health('http://127.0.0.1:18088/readyz');assert ai['ok']
        except BaseException:
            if original is None:SITE.unlink(missing_ok=True)
            else:replace(SITE,original)
            if not had_enabled:ENABLED.unlink(missing_ok=True)
            subprocess.run(['nginx','-t'],check=True);subprocess.run(['systemctl','reload','nginx'],check=True)
            raise
        result={'domain_ready':True,'domain':HOST,'proxy_commit':args.commit,'runtime_commit':runtime_commit,
                'model_container_restarted':False,'ai_container_restarted':False,'other_sites_byte_preserved':True,
                'wildcard_origin_certificate_hostname_checked':True,'site_sha256':digest(data),'site':str(SITE),
                'origin_tls_sni_hostname_verified':True,
                'public_health_url':'https://'+HOST+'/health','stream_url':'https://'+HOST+'/v1/stream',
                'voice_container_id':before_voice['Id'],'ai_container_id':before_ai['Id'],'ai_ready':ai['ok'],'receipt':str(receipt)}
        (receipt/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)

if __name__=='__main__':main()
