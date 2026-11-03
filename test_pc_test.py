"""PC test boundary/protocol checks use synthetic audio and private-value fixtures only. --yyl"""
import hashlib,hmac,http.client,json,pathlib,struct,tempfile,threading,unittest,uuid
from tools.pc_test import Cancelled,Job,Jobs,Settings,TestError,VoiceWire,make_server,sign

SECRET='test-only-pc-signing-fixture-do-not-deploy-00000'
PCM=b'\x01\x00\x00\x80\xff\x7f\x00\x00'
def frame(kind,value):
    data=value if isinstance(value,bytes)else json.dumps(value).encode()
    return struct.pack('>BI',kind,len(data))+data
def wire():
    return (b'XYVOICE1'+frame(1,{'rate':24000,'channels':1,'format':'pcm_s16le','voice_id':'a','total':2})+
        frame(2,{'segment':0,'total':2,'cached':False})+frame(3,PCM)+
        frame(2,{'segment':1,'total':2,'cached':True})+frame(3,PCM)+frame(4,{'complete':True,'segments':2}))

class WireTests(unittest.TestCase):
    def test_every_boundary_and_byte_fragments_preserve_actual_pcm_and_cache_flags(self):
        data=wire()
        for split in range(1,len(data)):
            decoder=VoiceWire('a');events=decoder.feed(data[:split])+decoder.feed(data[split:]);result=decoder.finish()
            self.assertEqual(b''.join(value for kind,value in events if kind=='pcm'),PCM*2)
            self.assertEqual(result['cached'],[False,True]);self.assertEqual(result['pcm_sha256'],hashlib.sha256(PCM*2).hexdigest())
        decoder=VoiceWire('a')
        for value in data:decoder.feed(bytes([value]))
        self.assertEqual(decoder.finish()['pcm_bytes'],len(PCM)*2)
    def test_truncated_stream_is_not_success_and_trailing_data_is_rejected(self):
        decoder=VoiceWire('a');decoder.feed(wire()[:-1])
        with self.assertRaises(TestError):decoder.finish()
        with self.assertRaises(TestError):VoiceWire('a').feed(wire()+b'x')
    def test_wrong_voice_pcm_order_and_odd_pcm_are_rejected(self):
        with self.assertRaises(TestError):VoiceWire('b').feed(wire())
        with self.assertRaises(TestError):VoiceWire('a').feed(b'XYVOICE1'+frame(3,PCM))
        value=wire().replace(frame(3,PCM),frame(3,b'x'),1)
        with self.assertRaises(TestError):VoiceWire('a').feed(value)
    def test_numeric_booleans_and_false_completion_are_not_valid_segments(self):
        data=wire().replace(frame(4,{'complete':True,'segments':2}),frame(4,{'complete':True,'segments':True}))
        with self.assertRaises(TestError):VoiceWire('a').feed(data)
        data=wire().replace(frame(4,{'complete':True,'segments':2}),frame(4,{'complete':False,'segments':2}))
        with self.assertRaises(TestError):VoiceWire('a').feed(data)
    def test_signing_covers_exact_unicode_body_owner_and_path(self):
        path,raw,headers=sign(SECRET,1987654321,'你好！','a')
        self.assertEqual(path,'/v1/stream?user_id=1987654321')
        payload='\n'.join(['POST',path,headers['X-Hht-Timestamp'],headers['X-Hht-Nonce'],hashlib.sha256(raw).hexdigest()])
        self.assertEqual(headers['X-Hht-Signature'],hmac.new(SECRET.encode(),payload.encode(),hashlib.sha256).hexdigest())
        self.assertEqual(json.loads(raw)['text'],'你好！')

class JobTests(unittest.TestCase):
    def test_new_runs_are_isolated_repeat_requires_completed_exact_voice_and_text(self):
        jobs=Jobs();first=jobs.begin(uuid.uuid4().hex,'一句话','a','fresh',None)
        with self.assertRaises(TestError):jobs.begin(uuid.uuid4().hex,'一句话','a','repeat',first.id)
        jobs.finish(first,True);second=jobs.begin(uuid.uuid4().hex,'一句话','a','repeat',first.id)
        self.assertEqual(second.uid,first.uid)
        other=jobs.begin(uuid.uuid4().hex,'一句话','a','fresh',None);self.assertNotEqual(other.uid,first.uid)
        with self.assertRaises(TestError):jobs.begin(uuid.uuid4().hex,'另一句话','a','repeat',first.id)
        with self.assertRaises(TestError):jobs.begin(uuid.uuid4().hex,'一句话','b','repeat',first.id)
    def test_cancellation_before_registration_does_not_start_an_upstream_run(self):
        jobs=Jobs();identity=uuid.uuid4().hex;jobs.cancel(identity)
        with self.assertRaises(Cancelled):jobs.begin(identity,'一句话','a','fresh',None)
    def test_stop_interrupts_saved_transport_when_http_connection_already_cleared_its_socket(self):
        class Transport:
            stopped=False
            def shutdown(self,how):self.stopped=True
        class Connection:
            sock=None
            closed=False
            def close(self):self.closed=True
        jobs=Jobs();first=jobs.begin(uuid.uuid4().hex,'一句话','a','fresh',None);second=jobs.begin(uuid.uuid4().hex,'一句话','a','fresh',None)
        first.connection=Connection();first.transport=Transport();jobs.cancel(first.id)
        self.assertTrue(first.transport.stopped);self.assertTrue(first.connection.closed)
        self.assertTrue(first.cancelled.is_set());self.assertFalse(second.cancelled.is_set())

class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();root=pathlib.Path(self.temporary.name)
        catalog={'default_voice_id':'a','voices':[{'id':'a','name':'测试音色','language':'zh','description':'fixture','reference_text':'private-reference-transcript'}]}
        (root/'catalog.json').write_text(json.dumps(catalog),encoding='utf-8')
        config={'secret':SECRET,'catalog_path':str(root/'catalog.json'),'model_dir':'private-model-path'}
        (root/'settings.json').write_text(json.dumps(config),encoding='utf-8')
        self.settings=Settings(root/'settings.json');self.server=make_server(self.settings,0)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True);self.thread.start()
        self.port=self.server.server_address[1]
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.temporary.cleanup()
    def request(self,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        raw=None if body is None else json.dumps(body).encode()
        connection.request('GET'if body is None else'POST',path,raw,headers or{})
        response=connection.getresponse();data=response.read();status=response.status;connection.close();return status,data
    def test_private_config_and_transcript_never_reach_the_page_or_public_configuration(self):
        for path in ['/','/api/config']:
            status,raw=self.request(path);self.assertEqual(status,200)
            for private in [SECRET.encode(),b'private-reference-transcript',b'private-model-path']:
                self.assertNotIn(private,raw)
        self.assertEqual(self.request('/ysim/runtime/xiaoya-voice.json')[0],404)
    def test_rebinding_cross_origin_and_unsigned_local_write_are_rejected(self):
        self.assertEqual(self.request('/api/config',headers={'Host':'attacker.invalid'})[0],421)
        self.assertEqual(self.request('/api/config',headers={'Origin':'https://attacker.invalid'})[0],403)
        self.assertEqual(self.request('/api/cancel',{'run_id':uuid.uuid4().hex},{'Content-Type':'application/json'})[0],403)
    def test_client_cannot_override_upstream_or_identity(self):
        headers={'Content-Type':'application/json','X-Voice-Test-Token':self.settings.token}
        body={'run_id':uuid.uuid4().hex,'text':'test','voice_id':'a','mode':'fresh','user_id':1,'url':'http://attacker.invalid'}
        self.assertEqual(self.request('/api/synthesize',body,headers)[0],400)
    def test_proxy_retains_audio_timeout_after_close_header_and_validates_full_stream(self):
        class Transport:
            timeout=10
            def settimeout(self,value):self.timeout=value
            def shutdown(self,how):pass
        class Response:
            status=200
            sent=False
            def getheader(self,name,default=None):return 'application/vnd.yilian.xiaoya-voice'if name=='Content-Type'else default
            def read1(self,size):
                if transport.timeout!=45:raise TimeoutError('Audio wait still uses the connection timeout')
                if self.sent:return b''
                self.sent=True;return wire()
            def close(self):pass
        class Connection:
            sock=None
            def connect(self):self.sock=transport
            def request(self,*args,**kwargs):pass
            def getresponse(self):self.sock=None;return Response()
            def close(self):pass
        transport=Transport();self.settings.connect=lambda:Connection()
        headers={'Content-Type':'application/json','X-Voice-Test-Token':self.settings.token}
        body={'run_id':uuid.uuid4().hex,'text':'test','voice_id':'a','mode':'fresh'}
        status,raw=self.request('/api/synthesize',body,headers);events=[json.loads(line)for line in raw.splitlines()]
        self.assertEqual(status,200);self.assertEqual(events[-1]['type'],'done')
        self.assertEqual(events[-1]['pcm_sha256'],hashlib.sha256(PCM*2).hexdigest())
        self.assertEqual(events[-1]['cached'],[False,True])

if __name__=='__main__':unittest.main()
