# 当前AI主机部署

2026-11-02部署到现有AMD EPYC-Rome 4vCPU、12GB内存主机，使用ZipVoice-Distill。原12份参考音色、温柔桃子升级版原A和合成参数保持。--yyl

2026-11-03使用独立语音域名 `voice.6xmu.com`。该变更仅添加专用Nginx站点并更新HHT调用基址，原模型镜像仍为 `65015625c2a094ff8d163930d6e747772238dd7c`，推理参数保持；路由版本由当前Git提交管理。--yyl

- 源码：[yylcreate/voice_server](https://github.com/yylcreate/voice_server)。模型版本以容器 `org.opencontainers.image.revision` 为准，路由版本以 `/opt/xiaoya-voice` 的Git提交为准；仅改域名时核对两者运行模块一致，无需重启模型。
- 健康：[https://voice.6xmu.com/health](https://voice.6xmu.com/health)。
- 合成：`POST https://voice.6xmu.com/v1/stream?user_id=<账号>`，必须使用后台HMAC签名，未签名请求返回403。
- 识别入口为 `POST /v1/transcribe?user_id=<账号>`，请求体为单声道 16 kHz PCM16 WAV，最长 30 秒，签名计算使用原始 WAV 字节。返回 `{"text":"…"}`。
- 程序：`/opt/xiaoya-voice`；私有模型：`/var/lib/xiaoya-voice/models/zipvoice`；私有配置：`/etc/xiaoya-voice/config.json`。
- Docker Compose项目 `xiaoya-voice`，容器 `xiaoya-voice-voice-1`；宿主端口仅 `127.0.0.1:8793`，经原Nginx站点提供HTTPS入口。
- CPU上限2.5核、内存上限4GiB，2条活动请求、单原生推理工作线程、2个模型线程、1个音频处理线程。第三条同时活动请求返回429；相同账号/音色/文字可复用进行中的计算。

本机HHT的私有配置 `url` 使用 `https://voice.6xmu.com`，签名密钥、模型路径和音色目录保留。原本机模型服务保留为回退；App继续调用业务后台，无需携带服务密钥或内置模型。其他机器部署HHT时，仍需私有设置相同的基础URL与密钥。

经试听确认的音色可在私有目录的 `voices` 条目设置 `high_shelf_db`，范围为 −4 到 0 dB，开启时同时配置 `output_lufs`。滤波在 3900 Hz 附近柔化高频，随后执行原响度和真峰值校准。缺少该字段的旧目录继续使用原处理流程。

选用完整 ZipVoice 的音色使用 `model_variant: full`。在已有私有模型目录的 `full/` 下准备 `text_encoder.onnx` 与 `fm_decoder.onnx`，并确认词表和声码器与当前前端兼容。仅在目录使用该档时加载这组权重。按目标主机的内存限制和首段返回时间实测后再启用。

启用识别前，在已挂载的模型目录内单独准备 SenseVoice INT8 的 `model.int8.onnx` 和 `tokens.txt`，在私有配置设置 `asr_model_dir`，例如 `/models/zipvoice/sensevoice`。模型文件与部署源码分开保存。核对健康页 `asr: true`，用签名 WAV 样本验收后，在手持端业务后台配置 `HHT_VOICE_ASR_ENABLED=1`。识别每次仅处理一路，繁忙时返回 429，模型使用一个 CPU 推理线程。并发资源和生产延迟需在目标服务器实测。

## 独立域名部署

当前Cloudflare代理记录指向同一AI主机。现有 `/etc/nginx/ssl/aichat.6xmu.com.pem` 实际为覆盖 `*.6xmu.com` 的Origin通配证书，专用站点复用它，不复制私钥。原AI站点及旧语音入口保留，专用域名只转发签名语音流和健康请求，其他路径404。

核对Git提交后运行 `sudo python3 deploy/setup_voice_domain.py --commit "$(git rev-parse HEAD)"`。脚本先检查证书域名/有效期、运行源码与当前镜像一致性，持有共享发布锁，保留现场并通过 `nginx -t` 后平滑重载。失败恢复本专用站点；不重启模型或AI容器。回执存于 `/var/lib/xiaoya-voice/domain-releases/`。

## 本次验证与性能边界

12个音色分别使用未命中缓存的合成请求，经公网HTTPS收齐PCM、逐段进度与完成帧。中文使用“你好！我是小雅，你的精密制造智能工作伙伴。”，Lauren使用对应的英文短句。首段4.698～9.447秒，中位6.055秒；完整返回5.544～10.607秒。结果包含网络和边缘代理时间，不等于模型独立推理时间，也不能外推到任意长度文本。

同期30秒只读资源观察平均占用1.442个CPU核，容器启动以来内存峰值约1.67GiB，未触发OOM。该观察是短时连续合成，不能当作持续压测或大量用户容量结论。4核共机仍有明显的新文本等待，尚未达到实时首响。

已验证：真实签名、未签名/重放拒绝、未知音色拒绝、两条活动请求时第三条429、关闭一条后另一条继续完成、缓存波形一致、后续新请求恢复，以及HHT PHP的证书校验和HTTP分块解码。手机实际播放和大量并发需单独验收。

完整新示例经原 `HandheldVoiceGateway` 异步代码与线上服务联测，首段5.249秒、完整15.248秒、3段共9.173秒音频，均未命中缓存。联测使用短时独立环回worker与合成测试用户，不调用登录或数据库接口，不代表手机实际播放验收。

发布只管理本语音容器。原AI容器及其代码、持久卷保持独立；Nginx仅添加语音的两个精确路由并通过配置检查后平滑重载。参考录音和模型不进入源码仓库、CI镜像或App包。
