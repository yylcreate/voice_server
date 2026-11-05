"""Private self-hosted TTS endpoint; signed backend requests, framed PCM response."""
from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing, asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import struct
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from voice_engine import VoiceEngine, segments
from voice_text import has_speech, mandarin_text, spoken_text
from voice_asr import MAX_WAV_BYTES, VoiceRecognizer, decode_wav
from voice_previews import load_previews
from runtime_profiles import resource_config, resource_info

MAGIC = b"XYVOICE1"
MEDIA = "application/vnd.yilian.xiaoya-voice"


def frame(kind: int, data: bytes | dict) -> bytes:
    if isinstance(data, dict):
        data = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    return struct.pack(">BI", kind, len(data)) + data


async def speech_parts(engine, owner, parts, voice_id, cancelled, disconnected):
    """At most one segment ahead, even when the PCM consumer applies backpressure."""
    pending = None
    current = None
    try:
        for index, part in enumerate(parts):
            if cancelled.is_set() or await disconnected():
                return
            if pending is None:
                pending = engine.synthesize(owner, part, cancelled, voice_id)
            current,pending=pending,None
            # 等当前原生推理结束后排下一段，与其响度处理重叠；先到的其他请求仍先执行。
            # native_done不表示PCM就绪；实际返回仍必须等待当前段的最终结果。--yyl
            await asyncio.wrap_future(getattr(current,'native_done',current))
            if cancelled.is_set() or await disconnected():return
            if index + 1 < len(parts):
                pending = engine.synthesize(owner, parts[index+1], cancelled, voice_id)
            result = await asyncio.wrap_future(current)
            current=None
            if cancelled.is_set() or await disconnected():
                return
            yield index, result
    finally:
        if pending is not None or current is not None:
            cancelled.set()
            for result in (current,pending):
                if result is not None:result.cancel()


async def wait_frames(parts, interval=6):
    """Emit a wait marker without cancelling the outstanding synthesis future."""
    pending = None
    try:
        while True:
            pending = asyncio.create_task(anext(parts))
            while not (await asyncio.wait({pending}, timeout=interval))[0]:
                yield None
            try:
                result = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield result
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def create_app(config: dict, engine=None, recognizer=None):
    config=resource_config(config)
    if len(config.get("secret", "")) < 32:
        raise ValueError("A service signing secret of at least 32 characters is required")
    engine = engine or VoiceEngine(config)
    nonces: dict[str, float] = {}
    jobs: dict[str, threading.Event] = {}
    if recognizer is None and config.get('asr_model_dir'):
        recognizer = VoiceRecognizer(config['asr_model_dir'])
    asr_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='xiaoya-asr') if recognizer else None
    asr_job = None
    previews = {}

    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.wrap_future(engine.warm())
            previews.update(load_previews(config, engine.voice_ids))
            if asr_worker:
                await asyncio.wrap_future(asr_worker.submit(recognizer.warm))
            yield
        finally:
            for event in jobs.values():
                event.set()
            if asr_worker:
                await asyncio.to_thread(asr_worker.shutdown, cancel_futures=True)
            await asyncio.to_thread(engine.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def reject(status, error):
        return JSONResponse({"error": error}, status_code=status, headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        # 返回资源预算供迁移核对；音色路径、文本和服务密钥保持私有。--yyl
        return JSONResponse({"ready": engine.ready, "active": len(jobs), "protocol": "XYVOICE1", "external_tts": False,
                             'asr': asr_worker is not None, 'voice_count': len(engine.voice_ids),
                             'preview_count': len(previews), 'runtime':resource_info(config)}, status_code=200 if engine.ready else 503)

    async def signed_body(request, maximum):
        raw = bytearray()
        async for piece in request.stream():
            raw.extend(piece)
            if len(raw) > maximum:
                return None, reject(413, "request too large")
        stamp = request.headers.get("x-hht-timestamp", "")
        nonce = request.headers.get("x-hht-nonce", "")
        signature = request.headers.get("x-hht-signature", "")
        target = request.url.path + ("?" + request.url.query if request.url.query else "")
        payload = "\n".join(["POST", target, stamp, nonce, hashlib.sha256(raw).hexdigest()])
        expected = hmac.new(config["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        now = time.time()
        for key, expiry in list(nonces.items()):
            if expiry < now:
                del nonces[key]
        if not (re.fullmatch(r"\d{10}", stamp) and abs(int(stamp)-now) <= 60 and
                re.fullmatch(r"[a-f0-9]{32}", nonce) and re.fullmatch(r"[a-f0-9]{64}", signature) and
                hmac.compare_digest(signature, expected) and nonce not in nonces):
            return None, reject(403, "invalid service signature")
        if len(nonces) >= 10000:
            return None, reject(429, "service busy")
        nonces[nonce] = now + 120
        return raw, None

    @app.post('/v1/preview')
    async def preview(request: Request):
        raw, error = await signed_body(request, 1024)
        if error is not None:
            return error
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {'voice_id'} or not isinstance(body['voice_id'], str):
                raise ValueError()
            if not re.fullmatch(r'[1-9][0-9]{0,18}', request.query_params.get('user_id', '')):
                raise ValueError()
        except ValueError:
            return reject(400, 'invalid preview request')
        audio = previews.get(body['voice_id'])
        if audio is None:
            return reject(404, 'preview not available')
        return Response(audio, media_type='audio/wav', headers={'Cache-Control': 'no-store'})

    @app.post('/v1/transcribe')
    async def transcribe(request: Request):
        nonlocal asr_job
        raw, error = await signed_body(request, MAX_WAV_BYTES)
        if error is not None:
            return error
        if not asr_worker:
            return reject(503, 'speech recognition not configured')
        if not re.fullmatch(r'[1-9][0-9]{0,18}', request.query_params.get('user_id', '')):
            return reject(400, 'invalid user')
        if asr_job is not None and not asr_job.done():
            return reject(429, 'speech recognition busy')
        try:
            samples = decode_wav(raw)
        except ValueError:
            return reject(400, 'invalid audio')
        asr_job = asr_worker.submit(recognizer.recognize, samples)
        try:
            result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(asr_job)), timeout=20)
        except TimeoutError:
            return reject(504, 'speech recognition timed out')
        except Exception:
            return reject(500, 'speech recognition failed')
        return JSONResponse({'text': result}, headers={'Cache-Control': 'no-store'})

    @app.post("/v1/stream")
    async def stream(request: Request):
        raw, error = await signed_body(request, 360000)
        if error is not None:
            return error
        try:
            body = json.loads(raw)
            required={"user_id", "request_id", "text"}
            if not isinstance(body, dict) or not required.issubset(body) or set(body)-required-{'voice_id', 'wait_frames'}:
                raise ValueError()
            keepalive = body.get('wait_frames', False)
            if type(keepalive) is not bool:
                raise ValueError()
            uid = body["user_id"]
            if type(uid) is not int or uid <= 0 or str(uid) != request.query_params.get("user_id"):
                raise ValueError()
            if not isinstance(body["request_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", body["request_id"]):
                raise ValueError()
            if not isinstance(body["text"], str) or not 1 <= len(body["text"].strip()) <= 60000:
                raise ValueError()
            voice_id=body.get('voice_id',engine.default_voice)
            if not isinstance(voice_id,str):raise ValueError()
            # 原直接交给模型的文本包含表情和格式符；清理后按受信音色路由。--yyl
            speaker = engine.default_voice if voice_id in ('female-03','lauren') else voice_id
            if speaker not in engine.voice_ids:raise ValueError()
            language = engine.voice_languages.get(speaker, 'zh')
            text = spoken_text(body['text'])
            # 原英文片段切换Lauren。现在中英混读都用所选音色，英文走模型内的英语前端。--yyl
            parts = [mandarin_text(part) if language == 'zh' else part
                     for part in segments(text, language=language, mode=engine.voice_modes.get(speaker,'default'))
                     if has_speech(part)]
            if not parts:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            return reject(400, "invalid speech request")
        if not engine.ready:
            return reject(503, "voice not ready")
        if body["request_id"] in jobs or len(jobs) >= int(config.get("max_streams", 4)):
            return reject(429, "service busy")
        cancelled = threading.Event()
        jobs[body["request_id"]] = cancelled

        async def audio():
            started = False
            magic = MAGIC
            delivered = 0
            try:
                async with aclosing(speech_parts(engine, uid, parts, speaker, cancelled, request.is_disconnected)) as prepared:
                    if keepalive:
                        yield magic + frame(6, {"waiting": True})
                        magic = b''
                    async with aclosing(wait_frames(prepared) if keepalive else prepared) as events:
                        async for event in events:
                            if event is None:
                                yield frame(6, {"waiting": True})
                                continue
                            index, result = event
                            if not started:
                                yield magic + frame(1, {"rate": result.rate, "channels": 1, "format": "pcm_s16le", "total": len(parts),'voice_id':voice_id})
                                started = True
                            yield frame(2, {"segment": index, "total": len(parts), "cached": result.cached})
                            for offset in range(0, len(result.data), 8192):
                                yield frame(3, result.data[offset:offset+8192])
                                if await request.is_disconnected():
                                    return
                            delivered = index + 1
                if delivered == len(parts) and not cancelled.is_set() and not await request.is_disconnected():
                    yield frame(4, {"complete": True, "segments": len(parts)})
            except asyncio.CancelledError:
                raise
            except Exception:
                if not started and not keepalive:
                    yield MAGIC
                yield frame(5, {"message": "语音生成失败，请重试"})
            finally:
                cancelled.set()
                jobs.pop(body["request_id"], None)

        return StreamingResponse(audio(), media_type=MEDIA, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app


def app_factory():
    path = Path(os.environ["XIAOYA_VOICE_CONFIG"])
    config = json.loads(path.read_text(encoding="utf-8"))
    # 只允许受信启动环境控制音频线程；仍由资源配置验证范围/固定AMD预算。--yyl
    audio_threads=os.environ.get('XIAOYA_VOICE_FFMPEG_THREADS')
    if audio_threads is not None:config['ffmpeg_threads']=int(audio_threads)
    # 仅服务进程的可信部署参数可选择资源预算，App请求不能覆盖。--yyl
    config=resource_config(config,os.environ.get('XIAOYA_VOICE_TARGET'))
    return create_app(config)
