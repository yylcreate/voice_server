import dataclasses,json,threading,unittest
from types import SimpleNamespace
import numpy as np
from voice_loudness import LoudnessNormalizer
from voice_profiles import VoiceProfile
from voice_engine import VoiceEngine,Pcm


class LoudnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.normalizer=LoudnessNormalizer()
        t=np.arange(72000,dtype=np.float32)/24000
        envelope=.45+.55*np.sin(2*np.pi*2.3*t)**2
        cls.signal=(.1*np.sin(2*np.pi*220*t)+.045*np.sin(2*np.pi*440*t)+.02*np.sin(2*np.pi*880*t))*envelope

    def test_quiet_and_normal_inputs_reach_same_loudness_without_clipping(self):
        outputs=[]
        for scale in(.08,1.0):
            result,report=self.normalizer.normalize(self.signal*scale,-16)
            measure=self.normalizer.measure(result)
            self.assertLess(abs(float(measure['input_i'])+16),.7)
            self.assertLessEqual(float(measure['input_tp']),-.9)
            self.assertLess(float(np.max(np.abs(result))),.96)
            self.assertLessEqual(abs(len(result)-len(self.signal)),240)
            self.assertTrue(np.isfinite(result).all());outputs.append(result)
        # Linear loudness matching must not alter the pitch or utterance duration.
        self.assertEqual(len(outputs[0]),len(outputs[1]))
        self.assertGreater(float(np.corrcoef(outputs[0],outputs[1])[0,1]),.995)

    def test_silent_nonfinite_and_invalid_target_fail(self):
        for samples,target in[(np.zeros(48000),-16),(np.array([np.nan]),-16),(self.signal,0),(self.signal,float('nan'))]:
            with self.assertRaises(ValueError):self.normalizer.normalize(samples,target)

    def test_short_acknowledgement_keeps_duration_and_has_audible_level(self):
        x=(.005*np.sin(2*np.pi*220*np.arange(4800)/24000)).astype(np.float32)
        result,report=self.normalizer.normalize(x,-16)
        self.assertEqual(len(result),len(x))
        self.assertAlmostEqual(float(np.sqrt(np.mean(result*result))),.16828,places=4)
        self.assertLessEqual(float(self.normalizer.measure(result)['input_tp']),-.9)
        self.assertEqual(report['mode'],'short-rms-with-truepeak-ceiling')

    def test_true_peak_limiter_handles_transients(self):
        source=self.signal*.2;source[::10000]=.9
        result,_=self.normalizer.normalize(source,-16)
        measured=self.normalizer.measure(result)
        self.assertLessEqual(float(measured['input_tp']),-.8)
        self.assertLess(abs(float(measured['input_i'])+16),1.0)

    def test_two_second_peak_limited_tail_does_not_collapse_in_volume(self):
        # 模拟约2秒尾句：语音能量正常，但少量瞬态接近峰值上限。--yyl
        t=np.arange(48640,dtype=np.float32)/24000
        x=(.15*np.sin(2*np.pi*220*t)+.04*np.sin(2*np.pi*440*t))*(.6+.4*np.sin(2*np.pi*2.3*t)**2)
        x[1000::12000]=.875
        before=self.normalizer.measure(x)
        output,report=self.normalizer.normalize(x,-16)
        after=self.normalizer.measure(output)
        self.assertEqual(len(output),len(x))
        self.assertGreater(float(np.sqrt(np.mean(output*output))),float(np.sqrt(np.mean(x*x)))*.9)
        self.assertLessEqual(float(after['input_tp']),-.9)
        self.assertGreater(float(after['input_i']),float(before['input_i'])-.5)
        self.assertEqual(report['mode'],'short-bs1770-with-truepeak-ceiling')

    def test_quiet_two_second_phrase_is_calibrated_without_timing_or_pitch_change(self):
        x=self.signal[:48640]*.08
        output,report=self.normalizer.normalize(x,-16)
        measured=self.normalizer.measure(output)
        self.assertEqual(len(output),len(x))
        self.assertGreater(float(np.corrcoef(x,output)[0,1]),.9999)
        self.assertLess(abs(float(measured['input_i'])+16),.6)
        self.assertLessEqual(float(measured['input_tp']),-.9)
        self.assertTrue(np.isfinite(output).all())
        self.assertGreater(report['gain_db'],0)

    def test_high_shelf_reduces_high_band_at_matched_loudness(self):
        for length in (48000,96000):
            with self.subTest(length=length):
                t=np.arange(length)/24000
                source=(.1*np.sin(2*np.pi*1000*t)+.05*np.sin(2*np.pi*7000*t)).astype(np.float32)
                normal,_=self.normalizer.normalize(source,-16)
                softer,_=self.normalizer.normalize(source,-16,high_shelf_db=-4)
                self.assertEqual(len(normal),len(softer))
                baseline=np.abs(np.fft.rfft(normal))
                candidate=np.abs(np.fft.rfft(softer))
                low=length*1000//24000;high=length*7000//24000
                difference=20*np.log10((candidate[high]/candidate[low])/(baseline[high]/baseline[low]))
                self.assertLess(difference,-2.8)
                self.assertGreater(difference,-4.5)
                report=self.normalizer.measure(softer)
                self.assertLess(abs(float(report['input_i'])+16),.6)
                self.assertLessEqual(float(report['input_tp']),-.9)

    def test_accepted_A_bypasses_processing_and_keeps_reference_and_four_steps(self):
        source=self.signal.astype(np.float32);calls=[]
        class Model:
            def generate(self,*args):
                calls.append(args)
                return SimpleNamespace(sample_rate=24000,samples=source)
        class Forbidden:
            def normalize(self,*args):raise AssertionError('Accepted A must bypass loudness processing')
        voice=VoiceProfile('xiaoya-a','温柔桃子（升级版）','zh','A', '原A参考文字',np.array([.1,.2],dtype=np.float32),'hash')
        engine=VoiceEngine({});engine.profiles={'xiaoya-a':voice};engine.models={('int8',1.0):Model()};engine.normalizer=Forbidden()
        try:
            pcm=engine._generate('原A试听文字','xiaoya-a')
            engine._generate('原A第二句','xiaoya-a')
            expected=np.clip(source*32767,-32768,32767).astype('<i2').tobytes()
            self.assertEqual(pcm.data,expected)
            self.assertEqual(calls[0],('原A试听文字',voice.text,voice.samples.tolist(),24000,1.0,4))
            self.assertIs(calls[0][2],calls[1][2])
        finally:engine.close()

    def test_deferred_loudness_has_same_pcm_as_synchronous_finish(self):
        source=self.signal.astype(np.float32)
        class Model:
            def generate(self,*args):return SimpleNamespace(sample_rate=24000,samples=source)
        voice=VoiceProfile('sample','Sample','zh','','参考文字',np.array([.1],dtype=np.float32),'hash',output_lufs=-16,high_shelf_db=-2.22)
        engine=VoiceEngine({});engine.profiles={'sample':voice};engine.voice_ids={'sample'}
        engine.models={('int8',1.0):Model()};engine.normalizer=self.normalizer
        try:
            synchronous=engine._generate('同一段原始音频','sample')
            deferred=engine.synthesize(1,'同一段原始音频',threading.Event(),'sample').result(timeout=5)
            self.assertEqual(deferred.data,synchronous.data)
            self.assertEqual(deferred.rate,synchronous.rate)
        finally:engine.close()

    def test_transcript_steps_and_loudness_invalidate_same_reference_cache(self):
        voice=VoiceProfile('xiaoya-a','A','zh','test','你好。',np.array([.1],dtype=np.float32),'same-reference')
        engine=VoiceEngine({});engine.profiles={'xiaoya-a':voice};calls=[]
        engine._generate=lambda text,identity,**_:(calls.append(identity)or Pcm(bytes([len(calls),0])*20,24000,1))
        try:
            for setting in[voice,dataclasses.replace(voice,text='你好呀。'),dataclasses.replace(voice,steps=8),dataclasses.replace(voice,output_lufs=-16),dataclasses.replace(voice,guidance_scale=3),dataclasses.replace(voice,model_variant='fp32'),dataclasses.replace(voice,speed=.98),dataclasses.replace(voice,prosody_mode='chat'),dataclasses.replace(voice,greeting_steps=8),dataclasses.replace(voice,output_lufs=-16,high_shelf_db=-2.22)]:
                engine.profiles['xiaoya-a']=setting
                self.assertFalse(engine.synthesize(1,'同一句',threading.Event(),'xiaoya-a').result().cached)
                self.assertTrue(engine.synthesize(1,'同一句',threading.Event(),'xiaoya-a').result().cached)
            self.assertEqual(len(calls),10)
        finally:engine.close()

    def test_greeting_override_keeps_regular_sentences_at_original_steps(self):
        source=self.signal.astype(np.float32);calls=[]
        class Model:
            def generate(self,*args):calls.append(args);return SimpleNamespace(sample_rate=24000,samples=source)
        voice=VoiceProfile('sample','Sample','zh','','参考文字',np.array([.1],dtype=np.float32),'hash',greeting_steps=8)
        engine=VoiceEngine({});engine.profiles={'sample':voice};engine.models={('int8',1.0):Model()}
        try:
            for text in ('你好！我是小雅。','您好，我是小雅。','当前工单已经完成。'):
                engine._generate(text,'sample')
            self.assertEqual([call[-1] for call in calls],[8,8,4])
        finally:engine.close()

if __name__=='__main__':unittest.main()
