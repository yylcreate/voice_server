# HHT后台接入兼容

业务后台跨主机调用时，继续通过原登录、AI权限和服务端HMAC鉴权。App不保存语音服务密钥。基础URL使用 `https://aichat.6xmu.com`，签名路径保持 `/v1/stream?user_id=<账号>`。--yyl

部分边缘代理会发送多条 `Server-Timing`。它是[列表型性能计时头](https://www.w3.org/TR/server-timing/#the-server-timing-header-field)，HHT原解码器的一律拒绝重复头会使合法音频流失败。[兼容补丁](hht-server-timing.patch)仅允许忽略重复的此计时头，其他重复头、传输编码、长度、压缩、音频类型、分块结束和完整帧校验保持。

补丁针对业务仓库的 `ysim/plugin/ysimplghhtapi/app/service/VoiceHttpStreamDecoder.php`。先在业务仓库审查并应用，再使用业务运行环境的PHP验证：

```sh
php /path/to/voice_server/integrations/test_http_decoder.php /path/to/ysim/plugin/ysimplghhtapi/app/service/VoiceHttpStreamDecoder.php
```

该检查覆盖代理响应的每个切分位置、逐字节输入、两条/三条大小写不同的计时头，以及重复格式头的拒绝。不执行登录、数据库或业务请求，也不需要服务密钥。部署完成后仍须从业务PHP环境验证真实HTTPS签名流，仅健康接口成功不代表音频接入完成。
