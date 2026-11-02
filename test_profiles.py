import hashlib,json,struct,tempfile,threading,unittest,wave
from pathlib import Path
import numpy as np
from fastapi.testclient import TestClient
from voice_profiles import load_profiles
from voice_engine import VoiceEngine,Pcm
from server import create_app
from test_server import FakeEngine,SECRET,signed

class ProfileTests(unittest.TestCase):
    def fixture(self,root):
        path=root/'reference.wav'
        with wave.open(str(path),'wb')as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000);f.writeframes(np.full(24000,1200,dtype='<i2').tobytes())
        voice={'id':'xiaoya-a','name':'A','language':'zh','description':'test','reference_file':path.name,'reference_text':'你好。','reference_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        catalog={'default_voice_id':'xiaoya-a','voices':[voice]};target=root/'catalog.json'
        target.write_text(json.dumps(catalog),encoding='utf-8')
        return {'model_dir':str(root),'catalog_path':str(target)},catalog,target
    def test_trusted_catalog_load_checksum_default_and_path_bounds(self):
        with tempfile.TemporaryDirectory()as folder:
            config,catalog,target=self.fixture(Path(folder))
            default,voices=load_profiles(config)
            self.assertEqual(default,'xiaoya-a');self.assertEqual(len(voices[default].samples),24000)
            catalog['voices'][0]['reference_sha256']='0'*64;target.write_text(json.dumps(catalog))
            with self.assertRaises(ValueError):load_profiles(config)
            catalog['voices'][0]['reference_file']='../outside.wav';target.write_text(json.dumps(catalog))
            with self.assertRaises(ValueError):load_profiles(config)
    def test_duplicate_and_missing_default_fail_closed(self):
        with tempfile.TemporaryDirectory()as folder:
            config,catalog,target=self.fixture(Path(folder));catalog['voices']*=2;target.write_text(json.dumps(catalog))
            with self.assertRaises(ValueError):load_profiles(config)
            catalog['voices']=catalog['voices'][:1];catalog['default_voice_id']='absent';target.write_text(json.dumps(catalog))
            with self.assertRaises(ValueError):load_profiles(config)
    def test_render_settings_are_bounded_and_not_client_controlled(self):
        with tempfile.TemporaryDirectory()as folder:
            config,catalog,target=self.fixture(Path(folder));entry=catalog['voices'][0]
            for field,value in[('steps',0),('steps',True),('guidance_scale',7),('guidance_scale',True),('output_lufs',-40),('output_lufs',float('nan')),('model_variant','../arbitrary'),('speed',True),('speed',float('nan')),('speed',1.5),('prosody_mode','arbitrary'),('greeting_steps',True),('greeting_steps',3),('greeting_steps',32)]:
                original=dict(entry);entry[field]=value;target.write_text(json.dumps(catalog))
                with self.assertRaises(ValueError):load_profiles(config)
                entry.clear();entry.update(original)
            entry.update(steps=8,guidance_scale=3,output_lufs=-16,greeting_steps=12);target.write_text(json.dumps(catalog))
            _,profiles=load_profiles(config)
            self.assertEqual((profiles['xiaoya-a'].steps,profiles['xiaoya-a'].guidance_scale,profiles['xiaoya-a'].output_lufs),(8,3,-16))
            self.assertEqual(profiles['xiaoya-a'].greeting_steps,12)
    def test_pcm_cache_is_separate_for_voice_and_owner(self):
        engine=VoiceEngine({});engine.voice_ids={'xiaoya-a','second'};calls=[]
        engine._generate=lambda text,voice,**_:(calls.append(voice)or Pcm(bytes([len(calls),0])*100,24000,1))
        try:
            a=engine.synthesize(1,'same',threading.Event(),'xiaoya-a').result()
            b=engine.synthesize(1,'same',threading.Event(),'second').result()
            self.assertNotEqual(a.data,b.data)
            self.assertTrue(engine.synthesize(1,'same',threading.Event(),'xiaoya-a').result().cached)
            self.assertFalse(engine.synthesize(2,'same',threading.Event(),'xiaoya-a').result().cached)
            self.assertEqual(calls,['xiaoya-a','second','xiaoya-a'])
        finally:engine.close()
    def test_signed_voice_id_round_trip_and_unknown_rejection(self):
        with TestClient(create_app({'secret':SECRET},FakeEngine()))as client:
            import uuid
            body={'user_id':10133,'request_id':uuid.uuid4().hex,'text':'你好。','voice_id':'second'}
            raw,headers=signed(body);reply=client.post('/v1/stream?user_id=10133',content=raw,headers=headers)
            self.assertEqual(reply.status_code,200)
            kind,size=struct.unpack('>BI',reply.content[8:13]);self.assertEqual(kind,1)
            self.assertEqual(json.loads(reply.content[13:13+size])['voice_id'],'second')
            body['voice_id']='../../arbitrary';raw,headers=signed(body)
            self.assertEqual(client.post('/v1/stream?user_id=10133',content=raw,headers=headers).status_code,400)

if __name__=='__main__':unittest.main()
