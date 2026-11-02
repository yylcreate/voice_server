# AMD四vCPU / 12GB 原生部署

默认部署使用仓库根目录的Docker Compose，模型及配置私有挂载。此目录保留不使用Docker时的systemd方案；两种方式仅选择一种，避免同时占用8793。--yyl

1. `/opt/xiaoya-voice` 检出已审核Git提交，建立Python3.12 `.venv`，安装 `requirements.txt`，系统安装FFmpeg。
2. 原模型、espeak数据及12份参考目录位于 `/var/lib/xiaoya-voice/models/zipvoice`，保留摘要和相对路径。
3. 按 `config.example.json` 私有准备 `/etc/xiaoya-voice/config.json`，填入与业务后台一致的签名密钥，并让独立系统账号 `xiaoya-voice` 可读。
4. 运行 `python deploy/check_target.py --config /etc/xiaoya-voice/config.json --require-target-hardware`，核对实际CPU/内存、模型/参考与资源预算。
5. 检查 `systemd-analyze verify deploy/amd-4vcpu-12gb/xiaoya-voice.service`，再安装并启用该单元。

该目标保持一个原生工作线程、一个音频后处理工作线程、2个推理计算线程、2条活动流及FFmpeg1线程。单元CPU配额250%、内存软限制3GiB/硬限制4GiB，只监听127.0.0.1:8793，启动等待预热。

4vCPU/12GB是初始共机保护预算。上线后分别测A/阿宝/较高步数音色、新文字与缓存、1/2并发和第三条限流，记录首PCM分位、完整率、供音空档、业务响应与资源峰值；不把健康或2条接入误称为两条模型同时实时合成。
