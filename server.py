"""Private self-hosted TTS endpoint; signed backend requests, framed PCM response."""
from __future__ import annotations
import asyncio
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
from fastapi.responses import JSONResponse, StreamingResponse

from voice_engine import VoiceEngine, segments
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


def create_app(config: dict, engine=None):
    config=resource_config(config)
    if len(config.get("secret", "")) < 32:
        raise ValueError("A service signing secret of at least 32 characters is required")
    engine = engine or VoiceEngine(config)
    nonces: dict[str, float] = {}
    jobs: dict[str, threading.Event] = {}

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.wrap_future(engine.warm())
        yield
        for event in jobs.values():
            event.set()
        await asyncio.to_thread(engine.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def reject(status, error):
        return JSONResponse({"error": error}, status_code=status, headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        # 返回资源预算供迁移核对；音色路径、文本和服务密钥保持私有。--yyl
        return JSONResponse({"ready": engine.ready, "active": len(jobs), "protocol": "XYVOICE1", "external_tts": False,
                             'runtime':resource_info(config)}, status_code=200 if engine.ready else 503)

    @app.post("/v1/stream")
    async def stream(request: Request):
        raw = bytearray()
        async for piece in request.stream():
            raw.extend(piece)
            if len(raw) > 360000:
                return reject(413, "request too large")
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
            return reject(403, "invalid service signature")
        if len(nonces) >= 10000:
            return reject(429, "service busy")
        nonces[nonce] = now + 120
        try:
            body = json.loads(raw)
            required={"user_id", "request_id", "text"}
            if not isinstance(body, dict) or not required.issubset(body) or set(body)-required-{'voice_id'}:
                raise ValueError()
            uid = body["user_id"]
            if type(uid) is not int or uid <= 0 or str(uid) != request.query_params.get("user_id"):
                raise ValueError()
            if not isinstance(body["request_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", body["request_id"]):
                raise ValueError()
            if not isinstance(body["text"], str) or not 1 <= len(body["text"].strip()) <= 60000:
                raise ValueError()
            voice_id=body.get('voice_id',engine.default_voice)
            if not isinstance(voice_id,str)or voice_id not in engine.voice_ids:raise ValueError()
            parts = segments(body["text"],language=engine.voice_languages.get(voice_id,'zh'),mode=engine.voice_modes.get(voice_id,'default'))
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
            delivered = 0
            try:
                async with aclosing(speech_parts(engine, uid, parts, voice_id, cancelled, request.is_disconnected)) as prepared:
                    async for index, result in prepared:
                        if not started:
                            yield MAGIC + frame(1, {"rate": result.rate, "channels": 1, "format": "pcm_s16le", "total": len(parts),'voice_id':voice_id})
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
                if not started:
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
