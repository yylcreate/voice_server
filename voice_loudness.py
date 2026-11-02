"""Two-pass BS.1770 loudness matching in memory; original accepted voice can bypass it.

FFmpeg handles K weighting, gated loudness and oversampled true-peak limiting.
No handset setting, pitch processing, temporary audio file or external service.
"""
from __future__ import annotations
import json,math,re,shutil,subprocess
from pathlib import Path
import numpy as np


class LoudnessNormalizer:
    VERSION='bs1770-two-pass-limiter-v3-short'

    def __init__(self,executable='ffmpeg',threads=None):
        resolved=shutil.which(str(executable))
        if not resolved or not Path(resolved).is_file():
            raise ValueError('Local FFmpeg is required for calibrated voices')
        self.executable=resolved
        if threads is not None and(type(threads)is not int or not 1<=threads<=8):
            raise ValueError('Invalid local audio thread budget')
        self.threads=threads

    def _run(self,samples,filter_text,measure_only=False):
        samples=np.asarray(samples,dtype='<f4')
        if samples.ndim!=1 or not len(samples)or not np.isfinite(samples).all():
            raise ValueError('Invalid loudness input')
        args=[self.executable,'-hide_banner','-nostdin','-loglevel','info']
        if self.threads is not None:
            # 限制四vCPU目标机的滤镜/解码线程，保留原响度算法和所有滤镜参数。--yyl
            args+=['-threads',str(self.threads),'-filter_threads',str(self.threads),'-filter_complex_threads',str(self.threads)]
        args+=['-f','f32le','-ar','24000','-ac','1','-i','pipe:0','-af',filter_text]
        if self.threads is not None:args+=['-threads',str(self.threads)]
        args+=['-f','null','-']if measure_only else ['-f','f32le','-acodec','pcm_f32le','-ar','24000','-ac','1','pipe:1']
        result=subprocess.run(args,input=samples.tobytes(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              timeout=20,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        matches=re.findall(rb'\{\s*"input_i"\s*:[^}]+\}',result.stderr)
        if not matches:raise ValueError('Missing local loudness measurement')
        report=json.loads(matches[-1])
        return result.stdout,report

    def measure(self,samples,target=-16.0):
        _,report=self._run(samples,f'loudnorm=I={target}:TP=-1.0:LRA=11:print_format=json',True)
        return report

    def normalize(self,samples,target=-16.0):
        if not isinstance(target,(float,int))or not math.isfinite(target)or not -24<=target<=-12:
            raise ValueError('Invalid loudness target')
        report=self.measure(samples,target)
        if len(samples)<9600:
            # Integrated BS.1770 needs a400ms gate; a short acknowledgement may
            # be shorter. Match the accepted voice's RMS with a measured true-peak
            # ceiling, without adding silence or changing that utterance's timing.
            x=np.asarray(samples,dtype=np.float32);rms=float(np.sqrt(np.mean(x*x)))
            peak=float(report['input_tp'])
            if rms<1e-6 or not math.isfinite(peak):raise ValueError('Generated audio has no measurable speech loudness')
            target_rms=.16828*10**((target+16)/20)
            gain_db=min(20*math.log10(target_rms/rms),-1.0-peak)
            output=(x*10**(gain_db/20)).astype(np.float32)
            return output,{'input_lufs':None,'output_lufs':None,'true_peak_dbfs':round(peak+gain_db,3),
                           'mode':'short-rms-with-truepeak-ceiling','gain_db':round(gain_db,3)}
        keys=('input_i','input_tp','input_lra','input_thresh','target_offset')
        if not all(math.isfinite(float(report[key]))for key in keys):
            raise ValueError('Generated audio has no measurable speech loudness')
        filters=(f'loudnorm=I={target}:TP=-1.0:LRA=11:linear=true:print_format=json'
                 f':measured_I={report["input_i"]}:measured_TP={report["input_tp"]}'
                 f':measured_LRA={report["input_lra"]}:measured_thresh={report["input_thresh"]}'
                 f':offset={report["target_offset"]}')
        # loudnorm limits at192kHz; subsequent downsampling can overshoot narrow
        # transients. Limit after the24kHz resampler too, with latency compensated.
        filters+=',aresample=24000,alimiter=limit=0.84:level=false:attack=5:release=50:latency=true'
        raw,after=self._run(samples,filters)
        output=np.frombuffer(raw,dtype='<f4').copy()
        if not len(output)or not np.isfinite(output).all()or float(np.max(np.abs(output)))>.96:
            raise ValueError('Invalid calibrated audio')
        if abs(len(output)-len(samples))>240:
            raise ValueError('Unexpected calibrated audio duration')
        measured=self.measure(output,target)
        actual_peak=float(measured['input_tp']);actual_i=float(measured['input_i'])
        if not math.isfinite(actual_peak)or not math.isfinite(actual_i):raise ValueError('Invalid calibrated loudness')
        correction=min(0.0,-1.0-actual_peak)
        if correction<0:output*=10**(correction/20)
        return output,{'input_lufs':float(report['input_i']),'output_lufs':round(actual_i+correction,3),
                       'true_peak_dbfs':round(actual_peak+correction,3),'mode':after['normalization_type'],
                       'final_peak_correction_db':round(correction,3)}
