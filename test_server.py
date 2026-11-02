import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import struct
import threading
import time
import unittest
import uuid

from fastapi.testclient import TestClient
from starlette.requests import Request
import numpy as np
from server import create_app, MAGIC, speech_parts
from voice_engine import Pcm, VoiceEngine, segments, _PcmInput

SECRET = "test-only-self-hosted-voice-signing-key-000000"


class FakeEngine:
    ready = True
    default_voice='xiaoya-a'
    voice_ids={'xiaoya-a','second'}
    voice_languages={'xiaoya-a':'zh','second':'en'}
    voice_modes={'xiaoya-a':'default','second':'default'}
    def warm(self):
        result = concurrent.futures.Future(); result.set_result(None); return result
    def synthesize(self, owner, text, cancelled,voice_id=None):
        result = concurrent.futures.Future(); result.set_result(Pcm(b"\x01\x00"*240, 24000, 1)); return result
    def close(self):
        pass


def signed(body, path="/v1/stream?user_id=10133", nonce=None):
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    stamp, nonce = str(int(time.time())), nonce or uuid.uuid4().hex
    message = "\n".join(["POST", path, stamp, nonce, hashlib.sha256(raw).hexdigest()])
    return raw, {"Content-Type":"application/json", "X-Hht-Timestamp":stamp,"X-Hht-Nonce":nonce,
                 "X-Hht-Signature":hmac.new(SECRET.encode(),message.encode(),hashlib.sha256).hexdigest()}


class VoiceTests(unittest.TestCase):
    def body(self):
        return {"user_id":10133,"request_id":uuid.uuid4().hex,"text":"你好，我是小雅。请核对数量。"}

    def test_signed_stream_has_metadata_pcm_and_explicit_completion(self):
        with TestClient(create_app({"secret":SECRET}, FakeEngine())) as client:
            raw, headers = signed(self.body())
            response = client.post("/v1/stream?user_id=10133",content=raw,headers=headers)
            self.assertEqual(response.status_code,200)
            data = response.content
            self.assertTrue(data.startswith(MAGIC))
            position, kinds = len(MAGIC), []
            while position < len(data):
                kind, size = struct.unpack(">BI",data[position:position+5]); position += 5
                payload = data[position:position+size]; position += size
                self.assertEqual(len(payload),size)
                kinds.append(kind)
                if kind == 1: self.assertEqual(json.loads(payload)["rate"],24000)
                if kind == 3: self.assertEqual(len(payload)%2,0)
            self.assertEqual(kinds[0],1)
            self.assertEqual(kinds[-1],4)
            self.assertEqual(kinds.count(3),1)

    def test_unsigned_replay_and_owner_mismatch_rejected(self):
        with TestClient(create_app({"secret":SECRET}, FakeEngine())) as client:
            self.assertEqual(client.post("/v1/stream?user_id=10133",json=self.body()).status_code,403)
            raw, headers = signed(self.body())
            self.assertEqual(client.post("/v1/stream?user_id=10133",content=raw,headers=headers).status_code,200)
            self.assertEqual(client.post("/v1/stream?user_id=10133",content=raw,headers=headers).status_code,403)
            body=self.body(); body["user_id"]=10134
            raw, headers=signed(body)
            self.assertEqual(client.post("/v1/stream?user_id=10133",content=raw,headers=headers).status_code,400)

    def test_limit_and_unexpected_fields_fail_closed(self):
        with TestClient(create_app({"secret":SECRET}, FakeEngine())) as client:
            body=self.body(); body["audio_url"]="http://localhost/private"
            raw,headers=signed(body)
            self.assertEqual(client.post("/v1/stream?user_id=10133",content=raw,headers=headers).status_code,400)
            self.assertEqual(client.post("/v1/stream?user_id=10133",content=b"x"*360001).status_code,413)

    def test_pcm_cache_does_not_cross_owners(self):
        engine=VoiceEngine({})
        calls=[]
        engine._generate=lambda text,voice_id=None,**_:(calls.append(text) or Pcm(b"\0\0"*100,24000,1))
        event=threading.Event()
        try:
            self.assertFalse(engine.synthesize(10133,"synthetic",event).result().cached)
            self.assertTrue(engine.synthesize(10133,"synthetic",event).result().cached)
            self.assertFalse(engine.synthesize(10134,"synthetic",event).result().cached)
            self.assertEqual(len(calls),2)
            event.set()
            with self.assertRaises(concurrent.futures.CancelledError): engine.synthesize(10133,"other",event).result()
        finally: engine.close()

    def test_cached_pcm_bypasses_busy_worker_but_other_owner_still_waits(self):
        engine=VoiceEngine({});started=threading.Event();release=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text)
            if text=='busy':
                started.set()
                if not release.wait(5):raise RuntimeError('Test worker was not released')
            return Pcm(text.encode()*2,24000,1)
        engine._generate=generate
        try:
            seed=engine.synthesize(10133,'cached',threading.Event()).result(timeout=2)
            busy=engine.synthesize(10134,'busy',threading.Event())
            self.assertTrue(started.wait(2))
            cached=engine.synthesize(10133,'cached',threading.Event())
            self.assertTrue(cached.done())
            self.assertTrue(cached.result().cached)
            self.assertEqual(cached.result().data,seed.data)
            other=engine.synthesize(10135,'cached',threading.Event())
            self.assertFalse(other.done())
            release.set();busy.result(timeout=2)
            self.assertFalse(other.result(timeout=2).cached)
            self.assertEqual(calls,['cached','busy','cached'])
        finally:
            release.set();engine.close()

    def test_cancelled_request_does_not_receive_cached_pcm(self):
        engine=VoiceEngine({});calls=[]
        engine._generate=lambda text,voice_id=None,**_:(calls.append(text) or Pcm(b'\0\0',24000,1))
        try:
            engine.synthesize(10133,'cached',threading.Event()).result(timeout=2)
            cancelled=threading.Event();cancelled.set()
            with self.assertRaises(concurrent.futures.CancelledError):
                engine.synthesize(10133,'cached',cancelled).result(timeout=2)
            self.assertEqual(calls,['cached'])
        finally:engine.close()

    def test_queued_duplicate_misses_generate_once_and_expired_pcm_is_replaced(self):
        engine=VoiceEngine({});release=threading.Event();started=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text)
            if text=='busy':
                started.set()
                if not release.wait(5):raise RuntimeError('Test worker was not released')
            return Pcm(b'\0\0'*32,24000,1)
        engine._generate=generate
        try:
            busy=engine.synthesize(10134,'busy',threading.Event());self.assertTrue(started.wait(2))
            first=engine.synthesize(10133,'same',threading.Event())
            second=engine.synthesize(10133,'same',threading.Event())
            release.set();busy.result(timeout=2)
            self.assertFalse(first.result(timeout=2).cached)
            self.assertTrue(second.result(timeout=2).cached)
            self.assertEqual(calls,['busy','same'])
            with engine.cache_lock:
                key=engine._cache_key(10133,'same','xiaoya-a')
                engine.cache[key]=(time.monotonic()-1,first.result())
            self.assertFalse(engine.synthesize(10133,'same',threading.Event()).result(timeout=2).cached)
            self.assertEqual(calls,['busy','same','same'])
            with engine.cache_lock:
                self.assertEqual(engine.cache_bytes,sum(len(entry[1].data)for entry in engine.cache.values()))
        finally:release.set();engine.close()

    def test_sentence_split_retains_numbers_and_every_character(self):
        text="你好，我是小雅。数量123456789012345678901234567890，尺寸12.34毫米。"
        parts=segments(text)
        self.assertLessEqual(len(parts[0]),28)
        self.assertEqual("".join(parts),text)
        self.assertTrue(any("12.34"in part for part in parts))
        self.assertEqual(segments("你好，我是小雅。我可以帮你查看工单，查询加工进度。"),
                         ["你好，我是小雅。我可以帮你查看工单，查询加工进度。"])

    # 进行中复用的交付顺序和独立取消，使用可控屏障而非耗时断言。--yyl
    def test_inflight_duplicate_returns_before_unrelated_queued_speech(self):
        engine=VoiceEngine({});a_started=threading.Event();a_release=threading.Event()
        b_started=threading.Event();b_release=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text)
            started,release=(a_started,a_release) if text=='a' else (b_started,b_release)
            started.set()
            if not release.wait(5):raise RuntimeError('Test worker was not released')
            return Pcm(text.encode()*2,24000,1)
        engine._generate=generate
        try:
            first=engine.synthesize(10133,'a',threading.Event());self.assertTrue(a_started.wait(2))
            unrelated=engine.synthesize(10134,'b',threading.Event())
            duplicate=engine.synthesize(10133,'a',threading.Event())
            a_release.set();self.assertTrue(b_started.wait(2))
            self.assertEqual(first.result(timeout=2).data,duplicate.result(timeout=2).data)
            self.assertFalse(unrelated.done());self.assertTrue(duplicate.result().cached)
            b_release.set();unrelated.result(timeout=2)
            self.assertEqual(calls,['a','b'])
        finally:a_release.set();b_release.set();engine.close()

    def test_inflight_cancelling_first_waiter_keeps_second_and_cache(self):
        engine=VoiceEngine({});started=threading.Event();release=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text);started.set()
            if not release.wait(5):raise RuntimeError('Test worker was not released')
            return Pcm(b'\1\0'*16,24000,1)
        engine._generate=generate
        try:
            token=threading.Event();first=engine.synthesize(10133,'a',token);self.assertTrue(started.wait(2))
            second=engine.synthesize(10133,'a',threading.Event())
            token.set();first.cancel();release.set()
            self.assertTrue(first.cancelled());self.assertEqual(second.result(timeout=2).data,b'\1\0'*16)
            self.assertTrue(engine.synthesize(10133,'a',threading.Event()).result(timeout=2).cached)
            self.assertEqual(calls,['a'])
        finally:release.set();engine.close()

    def test_all_queued_waiters_cancel_without_generation_or_cache(self):
        engine=VoiceEngine({});started=threading.Event();release=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text)
            if text=='busy':
                started.set()
                if not release.wait(5):raise RuntimeError('Test worker was not released')
            return Pcm(b'\0\0'*16,24000,1)
        engine._generate=generate
        try:
            busy=engine.synthesize(10134,'busy',threading.Event());self.assertTrue(started.wait(2))
            first=engine.synthesize(10133,'a',threading.Event());second=engine.synthesize(10133,'a',threading.Event())
            first.cancel();second.cancel();release.set();busy.result(timeout=2)
            engine.executor.submit(lambda:None).result(timeout=2)
            self.assertEqual(calls,['busy']);self.assertIsNone(engine._cached(engine._cache_key(10133,'a','xiaoya-a')))
            self.assertFalse(engine.synthesize(10133,'a',threading.Event()).result(timeout=2).cached)
        finally:release.set();engine.close()

    def test_all_running_waiters_cancel_discards_pcm_then_accepts_retry(self):
        engine=VoiceEngine({});started=threading.Event();release=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text);started.set()
            if not release.wait(5):raise RuntimeError('Test worker was not released')
            return Pcm(b'\0\0'*16,24000,1)
        engine._generate=generate
        try:
            first=engine.synthesize(10133,'a',threading.Event());self.assertTrue(started.wait(2))
            second=engine.synthesize(10133,'a',threading.Event());first.cancel();second.cancel()
            release.set();engine.executor.submit(lambda:None).result(timeout=2)
            self.assertIsNone(engine._cached(engine._cache_key(10133,'a','xiaoya-a')))
            self.assertFalse(engine.synthesize(10133,'a',threading.Event()).result(timeout=2).cached)
            self.assertEqual(calls,['a','a'])
        finally:release.set();engine.close()

    def test_inflight_failure_reaches_all_waiters_and_retry_is_fresh(self):
        engine=VoiceEngine({});started=threading.Event();release=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text);started.set()
            if not release.wait(5):raise RuntimeError('Test worker was not released')
            if len(calls)==1:raise ValueError('Synthetic synthesis failure')
            return Pcm(b'\0\0'*16,24000,1)
        engine._generate=generate
        try:
            first=engine.synthesize(10133,'a',threading.Event());self.assertTrue(started.wait(2))
            second=engine.synthesize(10133,'a',threading.Event());release.set()
            for result in (first,second):
                with self.assertRaisesRegex(ValueError,'Synthetic synthesis failure'):result.result(timeout=2)
            self.assertFalse(engine.synthesize(10133,'a',threading.Event()).result(timeout=2).cached)
            self.assertEqual(calls,['a','a']);self.assertEqual(engine.flights,{})
        finally:release.set();engine.close()

    def test_next_native_job_runs_during_audio_finish_and_duplicate_waits_for_pcm(self):
        engine=VoiceEngine({});started=threading.Event();release=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text)
            if text=='a':return _PcmInput(np.zeros(16,dtype=np.float32),-16,time.perf_counter())
            return Pcm(b'\2\0'*16,24000,1)
        def finish(prepared):
            started.set()
            if not release.wait(5):raise RuntimeError('Audio worker was not released')
            return Pcm(b'\1\0'*16,24000,1)
        engine._generate=generate;engine._finish_audio=finish
        try:
            first=engine.synthesize(10133,'a',threading.Event());self.assertTrue(started.wait(2))
            other=engine.synthesize(10134,'b',threading.Event())
            second=engine.synthesize(10133,'a',threading.Event())
            self.assertEqual(other.result(timeout=2).data,b'\2\0'*16)
            self.assertFalse(first.done());self.assertFalse(second.done());self.assertEqual(calls,['a','b'])
            release.set();self.assertEqual(first.result(timeout=2).data,second.result(timeout=2).data)
            self.assertTrue(second.result().cached);self.assertEqual(engine.flights,{})
        finally:release.set();engine.close()

    def test_cancelled_generation_does_not_remove_replacement_flight(self):
        engine=VoiceEngine({});started=threading.Event();release=threading.Event();calls=[]
        def generate(text,voice_id=None,**_):
            calls.append(text)
            if len(calls)==1:
                started.set()
                if not release.wait(5):raise RuntimeError('Test worker was not released')
            return Pcm(bytes([len(calls),0])*16,24000,1)
        engine._generate=generate
        try:
            abandoned=engine.synthesize(10133,'a',threading.Event());self.assertTrue(started.wait(2))
            abandoned.cancel()
            replacement=engine.synthesize(10133,'a',threading.Event())
            replacement_join=engine.synthesize(10133,'a',threading.Event());release.set()
            self.assertEqual(replacement.result(timeout=2).data,b'\2\0'*16)
            self.assertEqual(replacement_join.result(timeout=2).data,replacement.result().data)
            self.assertEqual(calls,['a','a']);self.assertEqual(engine.flights,{})
        finally:release.set();engine.close()

    def test_english_keeps_short_utterance_together_and_long_words_intact(self):
        preview="Hello, I'm Xiaoya. I can help you check work orders and production progress."
        self.assertEqual(segments(preview,language='en'),[preview])
        text=' '.join('word'+str(i)for i in range(100))
        parts=segments(text,language='en')
        self.assertEqual(' '.join(parts).split(),text.split())
        self.assertLessEqual(len(parts[0]),112)
        self.assertTrue(all(len(part)<=160 for part in parts))

    def test_chat_preserves_complete_questions_and_exact_business_text(self):
        text='我先帮你确认一下当前情况。你想先查看今天的工单，还是先了解各道工序的加工进度？'
        self.assertEqual(segments(text,mode='chat'),[text])
        self.assertGreater(len(segments(text)),1)
        numbered='工单34425目前完成97.5%，数量319107。请确认一下，是先看明细，还是先查看下一道工序？'
        self.assertEqual(''.join(segments(numbered,mode='chat')),numbered)
        self.assertIn('97.5%',''.join(segments(numbered,mode='chat')))

    def test_complete_short_sentence_wins_over_mid_clause_character_cut(self):
        text='你好！我是小雅，您的精密制造智能伙伴。我可以为您实时查询生产工单、追踪加工进度。今天有什么我可以帮您的？'
        self.assertEqual(segments(text),[
            '你好！我是小雅，您的精密制造智能伙伴。',
            '我可以为您实时查询生产工单、追踪加工进度。',
            '今天有什么我可以帮您的？'])
        self.assertEqual(segments(text,mode='chat')[0],text[:40])
        long=text+'您可以告诉我工单编号，我会继续为您查询。查询结果仅作参考，请先确认现场的实际情况，再继续下一步操作。'
        pieces=segments(long,mode='chat')
        self.assertEqual(''.join(pieces),long)
        self.assertEqual(pieces[0],text[:40])
        self.assertTrue(all(len(part)<=32 for part in pieces[1:]))
        self.assertTrue(all(part[-1] in '。！？' for part in pieces))

    def test_new_work_partner_preview_keeps_production_phrase_and_every_character(self):
        text='你好！我是小雅，你的精密制造智能工作伙伴。我可以为你实时查询生产工单、追踪加工进度。今天有什么我可以帮你的？'
        for mode in ('default','chat'):
            pieces=segments(text,mode=mode)
            self.assertEqual(''.join(pieces),text)
            self.assertTrue(all(piece.endswith(('。','！','？')) for piece in pieces))
            self.assertTrue(any('实时查询生产工单、追踪加工进度。' in piece for piece in pieces))
            self.assertNotIn('你好！',pieces)
            self.assertNotIn('您',''.join(pieces))

    def test_language_comes_from_trusted_voice_not_request(self):
        with TestClient(create_app({'secret':SECRET},FakeEngine()))as client:
            body=self.body();body.update(voice_id='second',text="Hello, I'm Xiaoya. I can help you check work orders and production progress.")
            raw,headers=signed(body);response=client.post('/v1/stream?user_id=10133',content=raw,headers=headers)
            self.assertEqual(response.status_code,200)
            kind,size=struct.unpack('>BI',response.content[8:13]);self.assertEqual(kind,1)
            self.assertEqual(json.loads(response.content[13:13+size])['total'],1)
            body['language']='en';raw,headers=signed(body)
            self.assertEqual(client.post('/v1/stream?user_id=10133',content=raw,headers=headers).status_code,400)


class PrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_ahead_is_queued_while_first_result_is_pending(self):
        calls=[];results=[];next_queued=asyncio.Event()
        class Engine:
            def synthesize(self,owner,text,cancelled,voice_id):
                calls.append(text);result=concurrent.futures.Future();results.append(result)
                result.native_done=concurrent.futures.Future();result.native_done.set_result(None)
                if len(calls)==2:next_queued.set()
                return result
        async def connected():return False
        event=threading.Event();stream=speech_parts(Engine(),1,['first','second','third'],'abao',event,connected)
        task=asyncio.create_task(anext(stream));await asyncio.wait_for(next_queued.wait(),2)
        self.assertEqual(calls,['first','second'])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(event.is_set());self.assertTrue(all(result.cancelled() for result in results))

    async def test_next_segment_waits_for_native_stage_before_joining_queue(self):
        calls=[];results=[]
        class Engine:
            def synthesize(self,owner,text,cancelled,voice_id):
                calls.append(text);result=concurrent.futures.Future();results.append(result)
                result.native_done=concurrent.futures.Future();return result
        async def connected():return False
        event=threading.Event();stream=speech_parts(Engine(),1,['first','second','third'],'abao',event,connected)
        task=asyncio.create_task(anext(stream));await asyncio.sleep(0)
        self.assertEqual(calls,['first'])
        results[0].native_done.set_result(None)
        for _ in range(4):await asyncio.sleep(0)
        self.assertEqual(calls,['first','second']);self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(all(result.cancelled() for result in results))

    async def test_disconnect_during_pcm_never_emits_completion_for_partial_reply(self):
        app=create_app({'secret':SECRET},FakeEngine())
        body={'user_id':10133,'request_id':uuid.uuid4().hex,'text':'第一段内容需要播放。第二段内容也需要播放。第三段内容还需要播放。'}
        raw,headers=signed(body)
        sent=False;disconnected=False
        async def receive():
            nonlocal sent
            if not sent:
                sent=True;return {'type':'http.request','body':raw,'more_body':False}
            if disconnected:return {'type':'http.disconnect'}
            await asyncio.Event().wait()
        scope={'type':'http','method':'POST','scheme':'http','path':'/v1/stream','root_path':'','query_string':b'user_id=10133','headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()],'server':('localhost',80)}
        endpoint=next(route.endpoint for route in app.routes if route.path=='/v1/stream')
        response=await endpoint(Request(scope,receive))
        chunks=[]
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if chunk[0]==3:disconnected=True
        kinds=[chunk[0] for chunk in chunks[1:]]
        self.assertIn(3,kinds);self.assertNotIn(4,kinds)

    async def test_one_ahead_before_consumer_requests_more_and_no_duplicate_synthesis(self):
        calls=[]
        class Engine:
            def synthesize(self,owner,text,cancelled,voice_id):
                calls.append(text)
                result=concurrent.futures.Future()
                result.set_result(Pcm(b'\0\0',24000,1))
                return result
        async def connected():return False
        event=threading.Event();stream=speech_parts(Engine(),1,['first','second','third','fourth'],'abao',event,connected)
        self.assertEqual((await anext(stream))[0],0)
        self.assertEqual(calls,['first','second'])
        self.assertEqual((await anext(stream))[0],1)
        self.assertEqual(calls,['first','second','third'])
        self.assertEqual([index async for index,_ in stream],[2,3])
        self.assertEqual(calls,['first','second','third','fourth'])

    async def test_closing_stream_cancels_pending_segment_and_never_queues_third(self):
        calls=[];next_result=concurrent.futures.Future()
        class Engine:
            def synthesize(self,owner,text,cancelled,voice_id):
                calls.append(text)
                if len(calls)>1:return next_result
                result=concurrent.futures.Future();result.set_result(Pcm(b'\0\0',24000,1));return result
        async def connected():return False
        event=threading.Event();stream=speech_parts(Engine(),1,['first','second','third'],'abao',event,connected)
        await anext(stream)
        await stream.aclose()
        self.assertTrue(event.is_set());self.assertTrue(next_result.cancelled())
        self.assertEqual(calls,['first','second'])


if __name__=="__main__": unittest.main()
