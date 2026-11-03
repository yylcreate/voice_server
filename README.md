# 小雅 ZipVoice 语音服务

小雅AI的自部署CPU语音后端，使用ZipVoice-Distill与已确认的12个参考音色。默认温柔桃子（升级版）保留原A参考、INT8/guidance1/四步/原速和原输出；其他音色保留已确认的参数及响度处理。--yyl

本仓库管理服务源码、检查和部署文件。模型、参考录音、目录转写及签名配置由服务器私有保存，部署时单独迁移；它们不进入Git、镜像或App安装包。

当前主机入口、目录、资源预算与实际短句测量见 [部署记录](deploy/production.md)。--yyl

语音正式域名为 `https://voice.6xmu.com`，状态入口为 [/health](https://voice.6xmu.com/health)。专用站点配置见 [Nginx模板](deploy/nginx.voice-domain.conf)，基础URL仅使用域名，保持原 `/v1/stream` 签名路径。--yyl

## 当前行为

- 常驻模型，单原生推理工作线程；支持单独音频线程完成原BS.1770响度和真峰值校验。
- 按账号、音色完整参数指纹与文字隔离的32MiB/五分钟PCM缓存；同一键的进行中计算复用，完成便分别交付。
- 请求独立取消；全部取消时跳过未开始任务，已进入原生模型的任务完成后丢弃。最多预取一段，完整PCM与完成帧是成功依据。
- `XYVOICE1`二进制PCM流，24kHz/单声道/16位。调用方提供时间戳、随机数和HMAC签名，App不持有服务密钥。
- 4vCPU/12GB共机目标：2个推理线程、2条活动流、FFmpeg1线程；容器上限2.5CPU、4GiB内存。此为资源预算，不能当作已达到实时容量。

当前42项自动检查覆盖协议、鉴权、账号/音色隔离、进行中复用、取消/重试、单段预取、完整流、原A与响度处理。首次新文字在CPU上仍需数秒；同文减少排队不能当作所有新文字的模型提速。

## Linux容器部署

需要Docker Engine及Compose插件。按已审核Git提交构建，不使用工作区临时文件覆盖服务。

1. 将仓库克隆到 `/opt/xiaoya-voice`，检出审核的完整提交。
2. 将原ZipVoice模型、espeak数据、12份参考及当前 `voice-catalog.json` 私有迁移到 `/var/lib/xiaoya-voice/models/zipvoice`，保留相对路径和摘要。运行时目录必须可供容器UID/GID `10001:10001`读取。
3. 按 [容器配置模板](deploy/docker/config.example.json) 准备 `/etc/xiaoya-voice/config.json`，通过私有方式填入与HHT一致的现有签名密钥。建议配置 `root:10001`、权限0640，目录0750；空密钥不能启动。
4. 忙碌的共机服务器优先下载同一提交通过CI的 `voice-image-<完整提交>` 制品，核对制品SHA256及内部 `image-manifest.json`，执行 `docker load --input image.tar.gz`，再运行 `python3 tools/verify_loaded_image.py --manifest image-manifest.json`。验证文件层、运行配置及版本，兼容Docker不同后端的ID表示。镜像不包含私有模型/配置。CI制品保留一天，不能当作长期备份。
5. 执行 [受控发布脚本](deploy/release.sh)，参数是本次完整提交；已加载相同revision的镜像时跳过服务器构建：

   ```sh
   cd /opt/xiaoya-voice
   sudo bash deploy/release.sh "$(git rev-parse HEAD)"
   ```

脚本核对源码状态、构建revision和私有资产配置，只更新 `xiaoya-voice` 项目的 `voice` 容器。启动等待就绪，失败恢复上一镜像；首次部署失败则停止本次容器。模型、配置和原有AI服务不属于容器更新内容。模型端口仅映射到宿主机 `127.0.0.1:8793`，共享AI服务的容器和持久卷不受本项目Compose管理。

健康检查：`curl --fail http://127.0.0.1:8793/health`。原生Linux/systemd部署模板保留在 [AMD目标目录](deploy/amd-4vcpu-12gb/README.md)，选择一种运行方式，避免同端口运行两份模型。

## 接入现有业务后台

原HHT业务接口处理登录、AI许可与个人音色设置，再签名调用本服务。后台设置 `HHT_VOICE_BASE_URL` 与 `HHT_VOICE_SERVICE_SECRET`；基础URL仅含协议/主机/可选端口，不能含路径。跨主机使用校验证书的HTTPS。

可将 [Nginx片段](deploy/nginx.voice.conf) 合并到现有HTTPS站点：仅新增精确的 `/v1/stream` 与 `/voice/health`，签名路径和query保持，关闭语音缓冲。检查已有路由冲突，备份原配置，通过 `nginx -t` 后平滑重载。未经签名的合成返回403。

使用边缘代理时，将片段放到实际接收回源请求的原站点 `server` 中；如现有回源使用HTTP80，也需在该块增加相同精确路由。保持原站点的证书、其他路由和鉴权配置。HHT的代理计时头兼容补丁及纯PHP检查见 [接入兼容说明](integrations/README.md)。签名检查工具使用明确的 `Xiaoya-Voice-Deployment-Probe/1.0` 客户端标识，测试不改变边缘访问策略。--yyl

`POST /v1/stream?user_id=<账号>` 接收 `user_id`、32位十六进制 `request_id`、`text` 和可选 `voice_id`。签名头为 `X-Hht-Timestamp`、`X-Hht-Nonce`、`X-Hht-Signature`；规范及可执行样例见 [签名流检查工具](tools/smoke_test.py) 与 [协议检查](test_server.py)。接口返回的 `cached=true` 也包含同次进行中计算的复用。

## 本地开发

Python3.12及FFmpeg：

```sh
python -m venv .venv
# 激活环境后
python -m pip install -r requirements-dev.txt
python -m unittest discover -p 'test_*.py' -v
python tools/check_source.py
```

这些自动检查不需要私有模型。真实合成需要另外准备私有资产及配置。Windows可执行 `./start-local.ps1 -ConfigPath <私有配置路径>`；默认寻找本项目 `.runtime/config.json`，实际模型路径按本机设置，停止/status脚本仅管理本项目记录的语音进程。

CI在Ubuntu24.04/Python3.12上运行检查并构建CPU容器，还会在实际运行镜像内检查完整响度/重采样/限幅链，不下载参考录音或私有模型。模型实现来自 [ZipVoice](https://github.com/k2-fsa/ZipVoice) / [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)，运行库版本见 `requirements.txt`；迁移原模型时保留其原始许可证。
