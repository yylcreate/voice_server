"""资源预算、并发门禁及原响度的行为验证。"""
import asyncio
import unittest

import numpy as np
from fastapi.testclient import TestClient
from starlette.requests import Request

from runtime_profiles import TARGET,resource_config,resource_info
from server import create_app
from test_server import FakeEngine,SECRET,signed
from voice_loudness import LoudnessNormalizer


class RuntimeProfileTests(unittest.TestCase):
    def test_target_replaces_resource_budget_but_retains_voice_and_service_inputs(self):
        original={'secret':SECRET,'threads':4,'max_streams':4,'model_dir':'private-model',
                  'catalog_path':'private-catalog','ffmpeg':'ffmpeg','custom':'retained'}
        before=dict(original);result=resource_config(original,TARGET)
        self.assertEqual(original,before)
        self.assertEqual((result['threads'],result['max_streams'],result['ffmpeg_threads']),(2,20,1))
        for key in ('secret','model_dir','catalog_path','ffmpeg','custom'):
            self.assertEqual(result[key],original[key])
        self.assertEqual(resource_config(result),result)
        self.assertEqual(resource_config({})['threads'],4)
        for value in (True,0,9,'2'):
            with self.assertRaises(ValueError):resource_config({'threads':value})
        with self.assertRaises(ValueError):resource_config({},'unknown-host')

    def test_health_exposes_only_budget_and_request_cannot_override_it(self):
        config={'secret':SECRET,'deployment_target':TARGET,'model_dir':'private-model'}
        with TestClient(create_app(config,FakeEngine()))as client:
            response=client.get('/health');self.assertEqual(response.status_code,200)
            body=response.json();self.assertEqual(body['runtime'],resource_info(config))
            self.assertNotIn(SECRET,response.text);self.assertNotIn('private-model',response.text)
            import uuid
            request={'user_id':10133,'request_id':uuid.uuid4().hex,'text':'你好。','threads':8}
            raw,headers=signed(request)
            self.assertEqual(client.post('/v1/stream?user_id=10133',content=raw,headers=headers).status_code,400)

    def test_single_audio_thread_keeps_loudness_duration_and_waveform(self):
        rate=24000;t=np.arange(rate*2,dtype=np.float32)/rate
        source=(.01*np.sin(2*np.pi*220*t)+.004*np.sin(2*np.pi*440*t))*(.7+.3*np.cos(2*np.pi*1.1*t))
        unrestricted=LoudnessNormalizer();limited=LoudnessNormalizer(threads=1)
        first,_=unrestricted.normalize(source,-16);second,_=limited.normalize(source,-16)
        np.testing.assert_allclose(first,second,rtol=0,atol=1e-6)
        self.assertEqual(len(first),len(second))
        measured=limited.measure(second)
        self.assertLess(abs(float(measured['input_i'])+16),.7)
        self.assertLessEqual(float(measured['input_tp']),-.9)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_accepts_twenty_jobs_rejects_next_and_recovers_after_close(self):
        app=create_app({'secret':SECRET,'deployment_target':TARGET},FakeEngine())
        endpoint=next(route.endpoint for route in app.routes if route.path=='/v1/stream')
        health=next(route.endpoint for route in app.routes if route.path=='/health')
        held=[]
        async def request():
            import uuid
            body={'user_id':10133,'request_id':uuid.uuid4().hex,'text':'你好。'}
            raw,headers=signed(body);sent=False
            async def receive():
                nonlocal sent
                if not sent:sent=True;return {'type':'http.request','body':raw,'more_body':False}
                await asyncio.Event().wait()
            scope={'type':'http','method':'POST','scheme':'http','path':'/v1/stream','root_path':'',
                   'query_string':b'user_id=10133','headers':[(k.lower().encode(),v.encode())for k,v in headers.items()],
                   'server':('localhost',80)}
            return await endpoint(Request(scope,receive))
        try:
            for i in range(20):
                response=await request();self.assertEqual(response.status_code,200)
                held.append(response)
                # 首帧后保持消费者背压，让活动名额真实占用且关闭过程有明确边界。--yyl
                await asyncio.wait_for(anext(response.body_iterator),3)
            self.assertEqual((await request()).status_code,429)
            import json
            self.assertEqual(json.loads((await health()).body)['active'],20)
        finally:
            for response in held:
                await asyncio.wait_for(response.body_iterator.aclose(),3)
        self.assertEqual(json.loads((await health()).body)['active'],0)
        replacement=await request();self.assertEqual(replacement.status_code,200)
        await asyncio.wait_for(anext(replacement.body_iterator),3)
        await asyncio.wait_for(replacement.body_iterator.aclose(),3)
        self.assertEqual(json.loads((await health()).body)['active'],0)


if __name__=='__main__':unittest.main()
