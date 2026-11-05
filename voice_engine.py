"""A persistent, single-owner TTS worker. No external synthesis API or audio files."""
from __future__ import annotations
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import wave
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from voice_profiles import MODEL_WEIGHTS, load_profiles
from voice_loudness import LoudnessNormalizer
from voice_text import breakpoints
from runtime_profiles import resource_config


def segments(text: str, limit: int = 40, language: str = 'zh', mode: str = 'default') -> list[str]:
    result = []
    text = text.strip()
    while text:
        chat=mode=='chat'and language=='zh'
        maximum = min((112 if not result else 160)if language=='en'else((48 if not result else 64)if chat else(28 if not result else limit)),len(text))
        # A tiny greeting cannot cover generation of the following segment.
        # Merge short sentences so playback has time to overlap the next call.
        # 原按字符切断会把小数、字母缩写与收尾引号拆开。--yyl
        sentence_ends, soft_ends, protected = breakpoints(text[:maximum + 28])
        boundaries = [end for end in sentence_ends if 3 <= end <= maximum]
        minimum=60 if language=='en'else 24 if chat else 20
        usable = [end for end in boundaries if end >= min(minimum,len(text))]
        if language=='zh' and not usable:
            # Prefer a complete short sentence to a hard character/phrase cut.
            # The old20-character floor skipped "...智能伙伴。" at19characters,
            # cutting the next sentence between 查询 and 生产工单. Keep tiny
            # greetings merged, and preserve complete chat questions when possible.
            usable=[end for end in boundaries if end>=12]
        # Keep the opening long enough to cover the following generation call.
        # Later complete sentences can be generated while the preceding sentence
        # plays instead of merging several sentences into an expensive segment.
        early=[]
        if language=='zh' and result and len(text)>28:
            early=[end for end in boundaries if 16<=end<=32]
        if chat and not usable and maximum<len(text):
            # Abao keeps a nearby complete sentence/question instead of restarting
            # its intonation at the old28-character boundary. Other voices unchanged.
            nearby = next((end for end in sentence_ends if maximum < end <= maximum + 24), None)
            if nearby is not None:usable=[nearby]
        end = early[0] if early else usable[-1] if usable else maximum
        if early:usable=early
        if not usable and end < len(text):
            commas = [end for end in soft_ends if (40 if language=='en'else 16) <= end <= maximum]
            if commas:
                end = commas[-1]
            elif language == 'zh':
                nearby = next((offset for offset in sentence_ends if end < offset <= end + 12), None)
                if nearby is not None:end = nearby
            if end in protected:
                start = end - 1
                while start in protected:
                    start -= 1
                if start:
                    end = start
        part, text = text[:end].strip(), text[end:].lstrip()
        if part:
            result.append(part)
    return result


@dataclass
class Pcm:
    data: bytes
    rate: int
    synthesis_ms: int
    cached: bool = False


@dataclass
class _PcmInput:
    samples: np.ndarray
    target_lufs: float
    started: float
    high_shelf_db: float = 0.0


class _SpeechFuture(concurrent.futures.Future):
    """独立取消的交付结果；native_done仅用于排下一段，绝不当作已有声音。--yyl"""
    def __init__(self):
        super().__init__()
        self.native_done=concurrent.futures.Future()

    def cancel(self):
        cancelled=super().cancel()
        if cancelled:self.native_done.cancel()
        return cancelled


class _SynthesisFlight:
    """同账号同音色的进行中计算；每个订阅者仍保留独立取消状态。--yyl"""
    def __init__(self, lock):
        self.lock = lock
        self.waiters = []
        self.future = None
        self.finished = False
        self.native_finished = False
        self.native_cancelled = False
        self.native_error = None

    def is_set(self):
        with self.lock:
            return all(future.cancelled() or event.is_set() for future, event in self.waiters)


class VoiceEngine:
    def __init__(self, config: dict):
        self.config = resource_config(config)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="xiaoya-voice")
        # 响度计算与下一段原生推理重叠，仍只允许一个原生模型调用。--yyl
        self.audio_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="xiaoya-audio")
        self.model = None
        self.models = {}
        self.normalizer = None
        self.reference = None
        self.ready = False
        self.cache: OrderedDict[tuple, tuple[float, Pcm]] = OrderedDict()
        self.cache_lock = threading.Lock()
        self.flight_lock = threading.RLock()
        self.flights = {}
        self.reference_inputs = {}
        self.cache_bytes = 0
        self.cache_limit = 32 * 1024 * 1024
        self.profile = "xiaoya-reference-v2-loudness-"+LoudnessNormalizer.VERSION
        self.default_voice = 'xiaoya-a'
        self.voice_ids = {'xiaoya-a'}
        self.voice_languages = {'xiaoya-a':'zh'}
        self.voice_modes = {'xiaoya-a':'default'}
        self.profiles = {}

    def warm(self):
        return self.executor.submit(self._warm)

    def _warm(self):
        import sherpa_onnx
        root = Path(self.config["model_dir"])
        self.default_voice,self.profiles=load_profiles(self.config)
        self.voice_ids=set(self.profiles)
        self.voice_languages={identity:voice.language for identity,voice in self.profiles.items()}
        self.voice_modes={identity:voice.prosody_mode for identity,voice in self.profiles.items()}
        default=self.profiles[self.default_voice]
        self.reference_text=default.text;self.reference=default.samples
        rules = ",".join(str(root / name) for name in ("phone.fst", "date.fst", "number.fst"))
        if any(v.output_lufs is not None for v in self.profiles.values()):
            self.normalizer=LoudnessNormalizer(self.config.get('ffmpeg','ffmpeg'),threads=self.config.get('ffmpeg_threads'))
        # Guidance is immutable in the native model. Share one resident instance per
        # distinct trusted guidance setting, while A keeps its original instance/config.
        for variant,guidance in sorted({(v.model_variant,v.guidance_scale)for v in self.profiles.values()}):
            encoder,decoder=MODEL_WEIGHTS[variant]
            cfg = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
                zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
                    tokens=str(root / "tokens.txt"), encoder=str(root/encoder),
                    decoder=str(root/decoder), vocoder=str(root / "vocos_24khz.onnx"),
                    lexicon=str(root / "lexicon.txt"), data_dir=str(root / "espeak-ng-data"),guidance_scale=guidance),
                num_threads=int(self.config.get("threads", 4)), provider="cpu", debug=False),
                rule_fsts=rules, max_num_sentences=1)
            if not cfg.validate():raise ValueError("Invalid model configuration")
            self.models[(variant,guidance)]=sherpa_onnx.OfflineTts(cfg)
        default_model=(default.model_variant,default.guidance_scale)
        self.model=self.models[default_model]
        # Warm kernels once before readiness, never on a handset or per request.
        self._generate("你好。")
        for setting in self.models:
            if setting!=default_model:
                voice=next(v for v in self.profiles.values()if(v.model_variant,v.guidance_scale)==setting)
                self._generate('你好，我是小雅。',voice.id)
        self.ready = True

    def _generate(self, text: str, voice_id=None, *, defer_loudness=False):
        start = time.perf_counter()
        voice=self.profiles[voice_id or self.default_voice]
        model=self.models[(voice.model_variant,voice.guidance_scale)]
        steps=voice.greeting_steps if voice.greeting_steps is not None and text.lstrip().startswith(('你好','您好')) else voice.steps
        # 原每次 voice.samples.tolist() 重复装箱；只复用相同参考的输入，采样及参数不改。--yyl
        reference_key = (voice.id, voice.sha256)
        if reference_key not in self.reference_inputs:
            self.reference_inputs[reference_key] = voice.samples.tolist()
        audio = model.generate(text, voice.text, self.reference_inputs[reference_key], 24000, voice.speed, steps)
        samples = np.asarray(audio.samples, dtype=np.float32)
        if audio.sample_rate != 24000 or not len(samples) or not np.isfinite(samples).all():
            raise ValueError("Invalid generated audio")
        if voice.output_lufs is not None:
            prepared=_PcmInput(samples,voice.output_lufs,start,voice.high_shelf_db)
            if defer_loudness:return prepared
            return self._finish_audio(prepared)
        return self._pack_pcm(samples,start)

    def _finish_audio(self,prepared):
        # 保留原两遍响度、真峰值校验和限幅，原A仍直接绕过。--yyl
        samples,_=self.normalizer.normalize(prepared.samples,prepared.target_lufs,high_shelf_db=prepared.high_shelf_db)
        return self._pack_pcm(samples,prepared.started)

    @staticmethod
    def _pack_pcm(samples,start):
        pcm = np.clip(samples * 32767, -32768, 32767).astype("<i2").tobytes()
        if len(pcm) > 4 * 1024 * 1024:
            raise ValueError("Generated segment exceeds limit")
        return Pcm(pcm, 24000, round((time.perf_counter() - start) * 1000))

    def synthesize(self, owner: int, text: str, cancelled: threading.Event, voice_id=None):
        voice_id=voice_id or self.default_voice
        if voice_id not in self.voice_ids:raise ValueError('Unknown voice')
        if cancelled.is_set():
            result=_SpeechFuture();result.cancel();return result
        # 命中缓存直接交付，避免已生成的语音仍等待原生推理；账号及音色指纹隔离保持。--yyl
        key=self._cache_key(owner,text,voice_id)
        cached=self._cached(key)
        if cached is not None:
            result=_SpeechFuture();result.native_done.set_result(None);result.set_result(cached);return result
        # 原每次直接 executor.submit 会让 A1、B、A2 中的 A2 等待无关的 B。
        # 同一缓存键共享进行中计算，完成即分别交付；不跨账号或音色共享。--yyl
        result=_SpeechFuture()
        with self.flight_lock:
            flight=self.flights.get(key)
            if flight is None or flight.is_set() or flight.finished:
                flight=_SynthesisFlight(self.flight_lock)
                self.flights[key]=flight
                flight.waiters.append((result,cancelled))
                flight.future=self.executor.submit(self._synthesize,owner,text,flight,voice_id)
                result.add_done_callback(lambda done:self._waiter_done(flight,done))
                flight.future.add_done_callback(lambda done:self._flight_done(key,flight,done))
            else:
                flight.waiters.append((result,cancelled))
                result.add_done_callback(lambda done:self._waiter_done(flight,done))
                if flight.native_finished:self._signal_native(result,flight)
        return result

    def _waiter_done(self,flight,result):
        if result.cancelled():
            with self.flight_lock:
                if flight.is_set():flight.future.cancel()

    def _flight_done(self,key,flight,producer):
        with self.flight_lock:
            if not flight.native_finished:
                flight.native_finished=True
                flight.native_cancelled=producer.cancelled()
                flight.native_error=None if producer.cancelled() else producer.exception()
                for result,_ in flight.waiters:self._signal_native(result,flight)
        # 原生工作线程返回后，响度线程可继续完成该段；原生线程立即处理下一段。--yyl
        if not producer.cancelled() and producer.exception() is None:
            next_stage=producer.result()
            if isinstance(next_stage,concurrent.futures.Future):
                with self.flight_lock:
                    flight.future=next_stage
                    if flight.is_set():next_stage.cancel()
                next_stage.add_done_callback(lambda done:self._flight_done(key,flight,done))
                return
        with self.flight_lock:
            flight.finished=True
            if self.flights.get(key) is flight:self.flights.pop(key)
            waiters=tuple(flight.waiters)
        for index,(result,cancelled) in enumerate(waiters):
            if result.done():continue
            try:
                if cancelled.is_set() or producer.cancelled():
                    result.cancel()
                elif producer.exception() is not None:
                    result.set_exception(producer.exception())
                else:
                    pcm=producer.result()
                    # 后到订阅者复用本次PCM，沿用缓存复用标记，不重复计算。--yyl
                    result.set_result(Pcm(pcm.data,pcm.rate,0,True) if index else pcm)
            except concurrent.futures.InvalidStateError:
                # 网络取消可与工作线程交付并发发生，不能影响其余订阅者。--yyl
                pass

    @staticmethod
    def _signal_native(result,flight):
        try:
            if flight.native_cancelled:result.native_done.cancel()
            elif flight.native_error is not None:result.native_done.set_exception(flight.native_error)
            else:result.native_done.set_result(None)
        except concurrent.futures.InvalidStateError:
            pass

    def _cache_key(self, owner, text, voice_id):
        voice=self.profiles.get(voice_id)
        fingerprint=voice.fingerprint if voice is not None else 'test-only'
        return (owner,self.profile,voice_id,fingerprint,hashlib.sha256(text.encode()).digest())

    def _cached(self,key):
        # 请求线程和合成线程共用LRU，锁内只读写缓存，不等待模型计算。--yyl
        with self.cache_lock:
            cached=self.cache.get(key)
            if cached and cached[0]>time.monotonic():
                self.cache.move_to_end(key)
                return Pcm(cached[1].data,cached[1].rate,0,True)
        return None

    def _synthesize(self, owner: int, text: str, cancelled: threading.Event, voice_id):
        if cancelled.is_set():
            raise concurrent.futures.CancelledError()
        key=self._cache_key(owner,text,voice_id)
        # 排队期间另一请求可能已生成同文，进入工作线程后仍复查一次。--yyl
        cached=self._cached(key)
        if cached is not None:return cached
        pcm = self._generate(text,voice_id,defer_loudness=True)
        if isinstance(pcm,_PcmInput):
            return self.audio_executor.submit(self._finish_and_cache,key,pcm,cancelled)
        return self._store_pcm(key,pcm,cancelled)

    def _finish_and_cache(self,key,prepared,cancelled):
        if cancelled.is_set():raise concurrent.futures.CancelledError()
        return self._store_pcm(key,self._finish_audio(prepared),cancelled)

    def _store_pcm(self,key,pcm,cancelled):
        if cancelled.is_set():
            raise concurrent.futures.CancelledError()
        with self.cache_lock:
            prior = self.cache.pop(key, None)
            if prior:
                self.cache_bytes -= len(prior[1].data)
            self.cache[key] = (time.monotonic() + 300, pcm)
            self.cache_bytes += len(pcm.data)
            while self.cache_bytes > self.cache_limit:
                self.cache_bytes -= len(self.cache.popitem(last=False)[1][1].data)
        return pcm

    def close(self):
        self.ready=False
        # 先等原生任务停止投递，再回收响度线程，最后在原生所有者线程释放模型。--yyl
        self.executor.submit(lambda:None).result(timeout=90)
        self.audio_executor.shutdown(wait=True,cancel_futures=True)
        def release():
            self.ready = False
            with self.cache_lock:
                self.cache.clear()
                self.cache_bytes = 0
            self.model = None
            self.models.clear();self.normalizer=None
            self.profiles.clear();self.reference=None
            self.reference_inputs.clear()
            with self.flight_lock:self.flights.clear()
            self.voice_languages.clear()
            self.voice_modes.clear()
        self.executor.submit(release).result(timeout=90)
        self.executor.shutdown(wait=True, cancel_futures=True)
