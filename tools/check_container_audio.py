"""在实际运行镜像内检查响度/重采样/限幅链，不需要私有模型或参考。--yyl"""
import json
import numpy as np
from voice_loudness import LoudnessNormalizer

normalizer=LoudnessNormalizer(threads=1)
signal=(.04*np.sin(2*np.pi*240*np.arange(48000)/24000)).astype(np.float32)
output,report=normalizer.normalize(signal,-16)
measured=normalizer.measure(output)
assert len(output)==len(signal)and np.isfinite(output).all()
assert abs(float(measured['input_i'])+16)<1
assert float(measured['input_tp'])<=-.8
print(json.dumps({'container_audio_verified':True,'samples':len(output),'sample_rate':24000,
                  'channels':1,'lufs':measured['input_i'],'true_peak_dbfs':measured['input_tp'],
                  'normalization_mode':report['mode']}))
