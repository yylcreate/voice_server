"""可信部署资源配置，与音色参考、步数及声线参数分开。"""
from __future__ import annotations

TARGET='amd-4vcpu-12gb'


def resource_config(config: dict, target: str | None = None) -> dict:
    selected=target if target is not None else config.get('deployment_target','local')
    if selected not in ('local',TARGET):
        raise ValueError('Unknown voice deployment target')
    result=dict(config)
    result['deployment_target']=selected
    if selected==TARGET:
        # 四vCPU主机还需运行业务服务，固定留出计算余量并缩短等待队列。--yyl
        result.update(threads=2,max_streams=2,ffmpeg_threads=1)
    else:
        result.setdefault('threads',4)
        result.setdefault('max_streams',4)
        result.setdefault('ffmpeg_threads',None)
    for key,maximum in (('threads',8),('max_streams',16),('ffmpeg_threads',8)):
        value=result[key]
        if key=='ffmpeg_threads'and value is None:continue
        if type(value)is not int or not 1<=value<=maximum:
            raise ValueError('Invalid voice resource setting: '+key)
    return result


def resource_info(config: dict) -> dict:
    # 健康页仅公布资源预算，不输出模型路径、参考文字或服务密钥。--yyl
    resolved=resource_config(config)
    return {'target':resolved['deployment_target'],'model_workers':1,'postprocess_workers':1,
            'inference_threads':resolved['threads'],'max_streams':resolved['max_streams'],
            'ffmpeg_threads':resolved['ffmpeg_threads']}
