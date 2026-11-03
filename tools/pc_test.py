"""Loopback-only PC tester for the real HTTPS voice service; signing stays on this PC. --yyl"""
from __future__ import annotations
import argparse,base64,hashlib,hmac,http.client,json,pathlib,re,secrets,socket,ssl,struct,sys,threading,time,uuid
from collections import OrderedDict
from dataclasses import dataclass,field
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

UPSTREAM='https://voice.6xmu.com'
HOST='voice.6xmu.com'
AGENT='Xiaoya-Voice-Deployment-Probe/1.0'
MAGIC=b'XYVOICE1'
MAX_PCM=32*1024*1024
PAGE=pathlib.Path(__file__).with_name('pc_test.html')
GREETING='你好！我是小雅，你的精密制造智能工作伙伴。我可以为你实时查询生产工单、追踪加工进度。今天有什么我可以帮你的？'

class TestError(Exception):
    def __init__(self,message,status=400):super().__init__(message);self.status=status
class Cancelled(Exception):pass

class VoiceWire:
    """Validate fragmentation, segment order, PCM format and explicit completion. --yyl"""
    def __init__(self,voice):
        self.voice=voice;self.pending=bytearray();self.magic=False;self.meta=None
        self.segment=-1;self.segment_bytes=0;self.pcm_bytes=0;self.complete=False;self.cached=[];self.digest=hashlib.sha256()
    def feed(self,piece):
        if self.complete and piece:raise TestError('完成帧之后仍有未知数据',502)
        self.pending.extend(piece);events=[]
        if not self.magic:
            if len(self.pending)<8:return events
            if self.pending[:8]!=MAGIC:raise TestError('线上返回的音频协议无效',502)
            del self.pending[:8];self.magic=True
        while len(self.pending)>=5:
            kind,size=struct.unpack('>BI',self.pending[:5])
            if size>4194304 or(kind!=3 and size>65536):raise TestError('线上音频帧超出限制',502)
            if len(self.pending)<size+5:break
            raw=bytes(self.pending[5:size+5]);del self.pending[:size+5]
            if self.complete:raise TestError('完成帧之后仍有未知数据',502)
            if kind==3:
                if self.meta is None or self.segment<0 or not raw or len(raw)%2:raise TestError('PCM数据或顺序无效',502)
                self.segment_bytes+=len(raw);self.pcm_bytes+=len(raw);self.digest.update(raw)
                if self.pcm_bytes>MAX_PCM:raise TestError('测试音频超过32MiB，请缩短文字',413)
                events.append(('pcm',raw));continue
            try:data=json.loads(raw)
            except (ValueError,UnicodeError):raise TestError('线上音频元信息无效',502)from None
            if not isinstance(data,dict):raise TestError('线上音频元信息无效',502)
            if kind==1:
                if(self.meta is not None or type(data.get('rate'))is not int or data['rate']!=24000 or type(data.get('channels'))is not int or data['channels']!=1 or
                   data.get('format')!='pcm_s16le' or data.get('voice_id')!=self.voice or
                   type(data.get('total'))is not int or not 1<=data['total']<=60000):raise TestError('线上音频格式不匹配',502)
                self.meta=data;events.append(('metadata',data))
            elif kind==2:
                if(self.meta is None or type(data.get('total'))is not int or data['total']!=self.meta['total'] or type(data.get('segment'))is not int or
                   data['segment']!=self.segment+1 or data['segment']>=self.meta['total'] or
                   type(data.get('cached'))is not bool or(self.segment>=0 and not self.segment_bytes)):
                    raise TestError('线上音频分段顺序无效',502)
                self.segment=data['segment'];self.segment_bytes=0;self.cached.append(data['cached']);events.append(('segment',data))
            elif kind==4:
                if(self.meta is None or data.get('complete')is not True or type(data.get('segments'))is not int or data['segments']!=self.meta['total'] or
                   self.segment+1!=self.meta['total'] or not self.segment_bytes or not self.pcm_bytes):
                    raise TestError('线上音频未完整返回',502)
                self.complete=True
                if self.pending:raise TestError('完成帧之后仍有未知数据',502)
            elif kind==5:raise TestError('线上语音生成失败，请重试',502)
            else:raise TestError('线上返回了未知音频帧',502)
        return events
    def finish(self):
        if not self.magic or self.pending or not self.complete:raise TestError('线上音频连接提前结束，未计为成功',502)
        return {'pcm_bytes':self.pcm_bytes,'audio_seconds':self.pcm_bytes/48000,'segments':len(self.cached),
                'cached':self.cached,'pcm_sha256':self.digest.hexdigest()}

def sign(secret,uid,text,voice):
    path='/v1/stream?user_id='+str(uid)
    body=json.dumps({'user_id':uid,'request_id':uuid.uuid4().hex,'text':text,'voice_id':voice},ensure_ascii=False,separators=(',',':')).encode()
    stamp=str(int(time.time()));nonce=uuid.uuid4().hex
    message='\n'.join(['POST',path,stamp,nonce,hashlib.sha256(body).hexdigest()])
    return path,body,{'Content-Type':'application/json','User-Agent':AGENT,'Accept-Encoding':'identity','Connection':'close',
        'X-Hht-Timestamp':stamp,'X-Hht-Nonce':nonce,'X-Hht-Signature':hmac.new(secret.encode(),message.encode(),hashlib.sha256).hexdigest()}

@dataclass(repr=False)
class Job:
    id:str
    uid:int
    fingerprint:str
    mode:str
    started:float=field(default_factory=time.perf_counter)
    cancelled:threading.Event=field(default_factory=threading.Event)
    connection:object=None
    transport:object=None
    state:str='running'
    lock:threading.Lock=field(default_factory=threading.Lock)
    def stop(self):
        self.cancelled.set()
        with self.lock:connection=self.connection;transport=self.transport
        if connection is not None:
            try:
                # HTTP Connection:close后getresponse可能清空connection.sock，保留实际传输以中断等待。--yyl
                transport=transport if transport is not None else connection.sock
                if transport is not None:transport.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            connection.close()

class Jobs:
    def __init__(self):self.lock=threading.Lock();self.items=OrderedDict();self.early={};self.used=set()
    def begin(self,identity,text,voice,mode,previous):
        if not isinstance(identity,str)or not re.fullmatch('[a-f0-9]{32}',identity):raise TestError('测试标识无效')
        fingerprint=hashlib.sha256((voice+'\0'+text).encode()).hexdigest()
        with self.lock:
            self.early={key:until for key,until in self.early.items()if until>time.monotonic()}
            if identity in self.early:raise Cancelled()
            if identity in self.items:raise TestError('此测试已发起，请重新测试',409)
            if mode=='repeat':
                old=self.items.get(previous)
                if old is None or old.state!='complete' or old.fingerprint!=fingerprint:raise TestError('文字或音色已变化，请发起新合成',409)
                uid=old.uid
            elif mode=='fresh':
                uid=1500000000+secrets.randbelow(500000000)
                while uid in self.used:uid=1500000000+secrets.randbelow(500000000)
                self.used.add(uid)
            else:raise TestError('测试方式无效')
            job=Job(identity,uid,fingerprint,mode);self.items[identity]=job
            while len(self.items)>64:
                key=next((key for key,value in self.items.items()if value.state!='running'),None)
                if key is None:break
                self.items.pop(key)
            return job
    def cancel(self,identity):
        if not isinstance(identity,str)or not re.fullmatch('[a-f0-9]{32}',identity):raise TestError('测试标识无效')
        with self.lock:
            job=self.items.get(identity)
            if job is None:
                if len(self.early)>=64:self.early.pop(next(iter(self.early)))
                self.early[identity]=time.monotonic()+15
        if job is not None and job.state=='running':job.stop()
        return job is not None
    def finish(self,job,success):
        with self.lock:
            if job.state!='failed':job.state='complete'if success and not job.cancelled.is_set()else'cancelled'if job.cancelled.is_set()else'failed'

class Settings:
    def __init__(self,path):
        config=json.loads(path.read_text(encoding='utf-8-sig'))
        self.secret=config.get('secret');catalog_path=pathlib.Path(config['catalog_path'])
        if not isinstance(self.secret,str)or len(self.secret)<32:raise TestError('本机签名配置无效')
        if catalog_path.stat().st_size>131072:raise TestError('本机音色目录过大')
        catalog=json.loads(catalog_path.read_text(encoding='utf-8-sig'));self.voices={}
        for row in catalog['voices']:
            identity=row.get('id');name=row.get('name');language=row.get('language')
            if(not isinstance(identity,str)or not re.fullmatch('[a-z0-9_-]{1,48}',identity)or identity in self.voices or
               not isinstance(name,str)or not 1<=len(name)<=48 or language not in('zh','en')):raise TestError('本机音色目录无效')
            self.voices[identity]={'id':identity,'name':name,'language':language,'description':str(row.get('description',''))[:100]}
        self.default=catalog['default_voice_id']
        if self.default not in self.voices or not 1<=len(self.voices)<=32:raise TestError('默认音色不可用')
        self.token=secrets.token_hex(32);self.jobs=Jobs();self.slots=threading.BoundedSemaphore(2)
    def public(self):return {'endpoint':UPSTREAM,'voices':list(self.voices.values()),'default_voice_id':self.default,
        'greeting':GREETING,'token':self.token,'max_characters':60000,'max_pcm_bytes':MAX_PCM,'protocol':'XYVOICE1'}
    def connect(self):return http.client.HTTPSConnection(HOST,443,timeout=10,context=ssl.create_default_context())

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='XiaoyaVoiceTest'
    def log_message(self,*args):pass
    @property
    def settings(self):return self.server.settings
    def safe_request(self,write=False):
        port=self.server.server_address[1];hosts={f'127.0.0.1:{port}',f'localhost:{port}'}
        if self.headers.get('Host')not in hosts:raise TestError('仅允许本机访问',421)
        origin=self.headers.get('Origin')
        if origin and origin not in {'http://'+host for host in hosts}:raise TestError('请求来源无效',403)
        if write and not hmac.compare_digest(self.headers.get('X-Voice-Test-Token',''),self.settings.token):raise TestError('本机测试会话已失效，请刷新页面',403)
    # headers为BaseHTTPRequestHandler解析后的HTTPMessage属性，发送方法独立命名。--yyl
    def send_headers(self,status,media,length=None):
        self.send_response(status);self.send_header('Content-Type',media);self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Connection','close')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if length is not None:self.send_header('Content-Length',str(length))
        else:self.send_header('Transfer-Encoding','chunked')
        self.end_headers();self.close_connection=True
    def json(self,status,value):
        raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode();self.send_headers(status,'application/json; charset=utf-8',len(raw));self.wfile.write(raw)
    def read_json(self):
        try:size=int(self.headers.get('Content-Length','0'))
        except ValueError:raise TestError('请求长度无效')from None
        if not 0<size<=360000:raise TestError('测试文字过长',413)
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise TestError('请求类型无效',415)
        try:value=json.loads(self.rfile.read(size))
        except (ValueError,UnicodeError):raise TestError('请求内容无效')from None
        if not isinstance(value,dict):raise TestError('请求内容无效')
        return value
    def do_GET(self):
        try:
            self.safe_request()
            if self.path=='/':
                raw=PAGE.read_bytes();self.send_headers(200,'text/html; charset=utf-8',len(raw));self.wfile.write(raw)
            elif self.path=='/api/config':self.json(200,self.settings.public())
            elif self.path=='/api/health':
                start=time.perf_counter();connection=self.settings.connect()
                try:
                    connection.request('GET','/health',headers={'User-Agent':AGENT,'Connection':'close','Accept-Encoding':'identity'})
                    response=connection.getresponse();raw=response.read(65537)
                    if len(raw)>65536 or response.status!=200:raise TestError('线上服务暂未就绪',503)
                    data=json.loads(raw)
                    self.json(200,{'ready':data.get('ready')is True,'active':data.get('active'),'runtime':data.get('runtime',{}),
                                  'endpoint':UPSTREAM,'elapsed_ms':round((time.perf_counter()-start)*1000,3)})
                finally:connection.close()
            else:self.json(404,{'error':'Not found'})
        except TestError as error:self.json(error.status,{'error':str(error)})
        except (OSError,ValueError,http.client.HTTPException):
            try:self.json(502,{'error':'连接线上服务失败，请重试检测'})
            except OSError:pass
    def do_POST(self):
        try:
            self.safe_request(True);body=self.read_json()
            if self.path=='/api/cancel':
                if set(body)!={'run_id'}:raise TestError('取消请求无效')
                self.json(200,{'stopped':True,'registered':self.settings.jobs.cancel(body['run_id'])});return
            if self.path!='/api/synthesize':self.json(404,{'error':'Not found'});return
            allowed={'run_id','text','voice_id','mode','previous_id'}
            if set(body)-allowed or not isinstance(body.get('text'),str)or not 1<=len(body['text'].strip())<=60000:raise TestError('请输入1～60000字的测试文字')
            voice=body.get('voice_id')
            if not isinstance(voice,str)or voice not in self.settings.voices:raise TestError('请选择可用音色')
            if not self.settings.slots.acquire(blocking=False):raise TestError('本机已有两路测试，请先停止或等待完成',429)
            try:
                job=self.settings.jobs.begin(body.get('run_id'),body['text'],voice,body.get('mode'),body.get('previous_id'))
                self.stream(job,body['text'],voice)
            finally:self.settings.slots.release()
        except Cancelled:self.json(409,{'error':'测试已停止'})
        except TestError as error:self.json(error.status,{'error':str(error)})
        except (OSError,ValueError,http.client.HTTPException):
            try:self.json(502,{'error':'本机测试连接失败，请重试'})
            except OSError:pass
    def stream(self,job,text,voice):
        succeeded=False;stream_started=False;response=None
        def emit(kind,**data):
            raw=json.dumps({'type':kind,'at_ms':round((time.perf_counter()-job.started)*1000,3),**data},ensure_ascii=False,separators=(',',':')).encode()+b'\n'
            self.wfile.write(('%x\r\n'%len(raw)).encode()+raw+b'\r\n');self.wfile.flush()
        try:
            self.send_headers(200,'application/x-ndjson; charset=utf-8');stream_started=True
            emit('start',run_id=job.id,mode=job.mode,endpoint=UPSTREAM)
            connection=self.settings.connect()
            with job.lock:job.connection=connection
            if job.cancelled.is_set():raise Cancelled()
            connection.connect()
            with job.lock:job.transport=connection.sock
            # 握手使用10秒预算，音频等待另设45秒；不能依赖getresponse后可能已清空的sock。--yyl
            if job.transport is not None:job.transport.settimeout(45)
            if job.cancelled.is_set():raise Cancelled()
            path,raw,headers=sign(self.settings.secret,job.uid,text,voice)
            connection.request('POST',path,body=raw,headers=headers);response=connection.getresponse()
            if job.cancelled.is_set():raise Cancelled()
            if response.status!=200:
                if response.status==429:raise TestError('线上服务忙：活动请求已达上限，请稍后重试',429)
                if response.status==403:raise TestError('线上签名请求被拒绝，请检查本机语音配置',403)
                raise TestError('线上服务返回HTTP '+str(response.status),response.status)
            if response.getheader('Content-Type','').strip().lower()!='application/vnd.yilian.xiaoya-voice' or response.getheader('Content-Encoding'):
                raise TestError('线上返回的音频类型无效',502)
            # if connection.sock is not None:connection.sock.settimeout(45) # Connection:close会令此分支失效。--yyl
            emit('connected',status=200);wire=VoiceWire(voice);first=None
            while True:
                if job.cancelled.is_set():raise Cancelled()
                piece=response.read1(65536)
                if not piece:break
                for kind,data in wire.feed(piece):
                    if job.cancelled.is_set():raise Cancelled()
                    if kind=='pcm':
                        if first is None:first=(time.perf_counter()-job.started)*1000
                        emit('pcm',audio=base64.b64encode(data).decode('ascii'),bytes=len(data))
                    else:emit(kind,**data)
            result=wire.finish()
            if job.cancelled.is_set():raise Cancelled()
            emit('done',**result,first_pcm_ms=round(first,3),complete_ms=round((time.perf_counter()-job.started)*1000,3))
            succeeded=True
        except Cancelled:
            try:emit('cancelled',message='已停止接收音频')
            except OSError:pass
        except Exception as error:
            if not job.cancelled.is_set():
                message=str(error)if isinstance(error,TestError)else'线上音频连接中断，请重试'
                try:emit('error',message=message,status=getattr(error,'status',502))
                except OSError:pass
                job.state='failed'
            job.stop()
        finally:
            if response is not None:response.close()
            if job.connection is not None:job.connection.close()
            with job.lock:job.connection=None;job.transport=None
            self.settings.jobs.finish(job,succeeded)
            if stream_started:
                try:self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                except OSError:pass

class PCServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True

def make_server(settings,port):
    server=PCServer(('127.0.0.1',port),Handler);server.settings=settings;return server

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=pathlib.Path,required=True);parser.add_argument('--port',type=int,default=8797);args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('Choose a port from 1024 to 65535')
    settings=Settings(args.config);server=make_server(settings,args.port)
    print(json.dumps({'listening':'http://127.0.0.1:'+str(args.port),'upstream':UPSTREAM,'voices':len(settings.voices),'signing_material_exposed':False}),flush=True)
    try:server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:pass
    finally:
        for job in list(settings.jobs.items.values()):
            if job.state=='running':job.stop()
        server.server_close()

if __name__=='__main__':main()
