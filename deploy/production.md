# 当前AI主机部署

2026-11-02部署到现有AMD EPYC-Rome 4vCPU、12GB内存主机，使用ZipVoice-Distill。原12份参考音色、温柔桃子升级版原A和合成参数保持。--yyl

- 源码：[yylcreate/voice_server](https://github.com/yylcreate/voice_server)，运行版本以容器 `org.opencontainers.image.revision` 与 `/opt/xiaoya-voice` 的完整Git提交对应。
- 健康：[https://aichat.6xmu.com/voice/health](https://aichat.6xmu.com/voice/health)。
- 合成：`POST https://aichat.6xmu.com/v1/stream?user_id=<账号>`，必须使用后台HMAC签名，未签名请求返回403。
- 程序：`/opt/xiaoya-voice`；私有模型：`/var/lib/xiaoya-voice/models/zipvoice`；私有配置：`/etc/xiaoya-voice/config.json`。
- Docker Compose项目 `xiaoya-voice`，容器 `xiaoya-voice-voice-1`；宿主端口仅 `127.0.0.1:8793`，经原Nginx站点提供HTTPS入口。
- CPU上限2.5核、内存上限4GiB，2条活动请求、单原生推理工作线程、2个模型线程、1个音频处理线程。第三条同时活动请求返回429；相同账号/音色/文字可复用进行中的计算。

本机HHT的私有配置仅将 `url` 从原环回地址切换到 `https://aichat.6xmu.com`，签名密钥、模型路径和音色目录保留。原本机模型服务保留为回退；App继续调用业务后台，无需携带服务密钥或内置模型。其他机器部署HHT时，仍需私有设置相同的基础URL与密钥。

## 本次验证与性能边界

12个音色分别使用未命中缓存的合成请求，经公网HTTPS收齐PCM、逐段进度与完成帧。中文使用“你好！我是小雅，你的精密制造智能工作伙伴。”，Lauren使用对应的英文短句。首段4.698～9.447秒，中位6.055秒；完整返回5.544～10.607秒。结果包含网络和边缘代理时间，不等于模型独立推理时间，也不能外推到任意长度文本。

同期30秒只读资源观察平均占用1.442个CPU核，容器启动以来内存峰值约1.67GiB，未触发OOM。该观察是短时连续合成，不能当作持续压测或大量用户容量结论。4核共机仍有明显的新文本等待，尚未达到实时首响。

已验证：真实签名、未签名/重放拒绝、未知音色拒绝、两条活动请求时第三条429、关闭一条后另一条继续完成、缓存波形一致、后续新请求恢复，以及HHT PHP的证书校验和HTTP分块解码。手机实际播放和大量并发需单独验收。

完整新示例经原 `HandheldVoiceGateway` 异步代码与线上服务联测，首段5.249秒、完整15.248秒、3段共9.173秒音频，均未命中缓存。联测使用短时独立环回worker与合成测试用户，不调用登录或数据库接口，不代表手机实际播放验收。

发布只管理本语音容器。原AI容器及其代码、持久卷保持独立；Nginx仅添加语音的两个精确路由并通过配置检查后平滑重载。参考录音和模型不进入源码仓库、CI镜像或App包。
