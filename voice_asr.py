"""Local SenseVoice recognition for the handset's mono 16 kHz WAV."""
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import wave

import numpy as np

RATE = 16000
MAX_SAMPLES = 30 * RATE
MAX_WAV_BYTES = MAX_SAMPLES * 2 + 4096


def decode_wav(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            frames = audio.getnframes()
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, RATE):
                raise ValueError('Expected 16 kHz mono PCM16 WAV')
            if not 1 <= frames <= MAX_SAMPLES:
                raise ValueError('Expected up to 30 seconds of audio')
            pcm = audio.readframes(frames)
            if len(pcm) != 2 * frames:
                raise ValueError('Truncated PCM')
    except (wave.Error, EOFError) as error:
        raise ValueError('Invalid WAV') from error
    return np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768


class VoiceRecognizer:
    def __init__(self, model_dir):
        self.root = Path(model_dir)
        self.model = None

    def warm(self):
        from sherpa_onnx import OfflineRecognizer

        model = self.root / 'model.int8.onnx'
        tokens = self.root / 'tokens.txt'
        if not model.is_file() or not tokens.is_file():
            raise ValueError('SenseVoice model and tokens are required')
        self.model = OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), language='auto', use_itn=True, num_threads=1, provider='cpu')

    def recognize(self, samples):
        if np.max(np.abs(samples)) < 0.001:
            return ''
        stream = self.model.create_stream()
        stream.accept_waveform(RATE, samples)
        self.model.decode_stream(stream)
        return stream.result.text.strip()


class RecognitionWorker:
    """Each native recognizer belongs to one dedicated inference thread."""

    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='xiaoya-asr')
        self.pending = {}

    def workload(self):
        self.pending = {job: size for job, size in self.pending.items() if not job.done()}
        return sum(self.pending.values())

    def submit(self, samples):
        job = self.executor.submit(self.recognizer.recognize, samples)
        self.pending[job] = len(samples)
        return job
