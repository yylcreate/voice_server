# 腾讯云 CentOS 7 / 4核16G / 宝塔11.1：ZipVoice部署操作单

本教程对应用户确认的国内腾讯云机器。保留ZipVoice-Distill、原12个音色、温柔桃子升级版A及合成参数；当前CPU/内存通过原4核/至少11000MiB的资源门槛。内核、Docker和真实合成仍需按下面步骤在该机核验。--yyl

先完成第1～8步，通过SSH用浏览器测速；第9～11步再配置公网HTTPS。国内服务与当前voice.6xmu.com独立，原服务保持。

## 1. 确认机器并打开宝塔终端

1. 腾讯云控制台找到国内实例，记录公网IPv4、SSH端口、登录用户名。
2. 登录宝塔11.1，在首页核对4核/16G；点击左侧“终端”连接本机。
3. 输入以下命令。不是root时，先执行sudo -i：

~~~bash
whoami
cat /etc/centos-release
uname -m
uname -r
nproc
free -m
df -h / /var/lib/docker
~~~

期望root、x86_64、4个CPU及约16GB内存。最后一条若提示/var/lib/docker不存在，在Docker安装后再看磁盘；需要给镜像、模型及临时迁移包预留空间。

宝塔终端入口参考[官方终端说明](https://docs.bt.cn/user-guide/xterm/)。

## 2. 腾讯云和宝塔放行访问

CVM：腾讯云控制台 → 云服务器 → 实例 → 安全组 → 所关联安全组 → 修改规则 → 入站规则 → 添加规则。

如果购买的是轻量应用服务器，则进入该实例“防火墙”添加对应规则。

| TCP端口 | 来源 | 用途 |
| --- | --- | --- |
| 实际SSH端口，通常22 | 你的电脑公网IP/32 | SSH登录、SFTP、隧道 |
| 实际宝塔面板端口 | 你的电脑公网IP/32，保留原规则 | 宝塔管理 |
| 80 | 0.0.0.0/0，做域名时放行 | HTTP/证书验证 |
| 443 | 0.0.0.0/0，做域名时放行 | HTTPS语音接口 |

策略选“允许”，备注填写用途。先只走SSH测速时，80/443可留到第9步。

在宝塔“安全 → 系统防火墙/端口规则”核对同样的端口和来源。已有规则直接沿用，不关闭防火墙，不放通全部端口。8793、8797、8798、18793不向公网放行。

依据：[腾讯云安全组添加规则](https://cloud.tencent.com/document/product/213/112614)、[宝塔端口规则](https://docs.bt.cn/user-guide/security/firewall/port-rule/)。

## 3. 安装Docker，准备宿主管理工具

宝塔左侧“Docker” → 如果提示未安装，点击安装Docker并等待任务完成；旧入口可在“软件商店”搜索“Docker管理器”。已正常运行Docker时沿用，不点击卸载/重装。宝塔的安装任务包含Docker及Compose，见[官方安装说明](https://docs.bt.cn/api/docker/install-docker-program/)。

在宝塔终端执行：

~~~bash
docker version
docker compose version
docker info --format 'Driver={{.Driver}} Security={{json .SecurityOptions}}'
~~~

docker version应同时显示Client和Server，docker compose version应正常返回版本。只有docker-compose旧命令而docker compose不可用时，先补齐面板的Compose插件再继续。CentOS7安装源与内核较旧，不套用Ubuntu的apt命令，也不直接强行升级Docker。

继续执行：

~~~bash
yum install -y git python3 curl tar gzip util-linux
python3 --version
command -v git python3 curl tar gzip flock
install -d -m 700 /root/xiaoya-voice-transfer
~~~

宿主Python3只用于标准库管理脚本。模型及后面的签名测速使用镜像内Python3.12，不替换系统Python，不在宝塔Python项目管理器启动第二份模型。

若yum提示镜像源失效，保留错误后按当前腾讯云CentOS7镜像源处理，不直接覆盖全机yum仓库。已装工具不必重复安装。

## 4. 从原线上机器导出迁移文件

当前Git仓库不包含私有模型、12份参考及密钥，必须迁移这些文件。

在自己Windows电脑新开PowerShell，使用原语音服务器的既有IPv6地址或SSH别名登录。替换下面的地址占位文字：

~~~powershell
ssh -6 root@原语音服务器IPv6地址
~~~

使用原主机的现有SSH凭据登录，凭据不用写进命令或发到网页。确认进入原服务器后，完整执行：

~~~bash
set -Eeuo pipefail
umask 077
cd /opt/xiaoya-voice

voice_image=$(docker inspect --format '{{.Config.Image}}' xiaoya-voice-voice-1)
voice_commit=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$voice_image")
[[ "$voice_commit" =~ ^[a-f0-9]{40}$ ]]
test "$voice_image" = "xiaoya-voice:$voice_commit"
git cat-file -e "$voice_commit^{commit}"

voice_package=$(mktemp -d /root/xiaoya-voice-cn.XXXXXXXX)
GITHUB_SHA="$voice_commit" python3 tools/package_ci_image.py
install -m 600 .ci-artifact/image.tar.gz "$voice_package/image.tar.gz"
install -m 600 .ci-artifact/image-manifest.json "$voice_package/image-manifest.json"
git bundle create "$voice_package/source.bundle" --all
printf '%s\n' "$voice_commit" > "$voice_package/commit.txt"

tar -czf "$voice_package/zipvoice-assets.tar.gz" \
  -C /var/lib/xiaoya-voice/models/zipvoice .
install -m 600 /etc/xiaoya-voice/config.json "$voice_package/config.json"

cd "$voice_package"
sha256sum image.tar.gz image-manifest.json source.bundle \
  commit.txt zipvoice-assets.tar.gz config.json > SHA256SUMS
printf '需要迁移的目录：%s\n' "$voice_package"
~~~

记住最后输出的目录。导出使用当前实际运行镜像的revision，不停止原服务，也不把最近文档提交误当模型镜像版本。镜像迁移依据[Docker save/load](https://docs.docker.com/reference/cli/docker/image/load/)。

## 5. 将7个文件上传到国内宝塔

用SFTP工具连接原服务器，进入第4步输出的目录，把7个文件下载到自己电脑的私有目录：

- image.tar.gz
- image-manifest.json
- source.bundle
- commit.txt
- zipvoice-assets.tar.gz
- config.json
- SHA256SUMS

然后打开国内宝塔：左侧“文件” → 地址栏填/root/xiaoya-voice-transfer → 回车 → “上传” → 选中这7个文件 → 等待全部完成。上传大文件失败时，用SFTP上传同一个目录。先不要在面板内随意解压镜像或修改config.json。

这7个文件中的config.json含签名值，模型包含私有参考与转写；整个目录留在/root私有区域，不放入/www/wwwroot，不上传Git或公开下载地址。

## 6. 国内宝塔导入并启动

回到国内宝塔“终端”，确认root后完整执行：

~~~bash
set -Eeuo pipefail
umask 077
cd /root/xiaoya-voice-transfer
chmod 700 /root/xiaoya-voice-transfer
chmod 600 image.tar.gz image-manifest.json source.bundle \
  commit.txt zipvoice-assets.tar.gz config.json SHA256SUMS
sha256sum -c SHA256SUMS

# 首次部署：任何目标已经存在时停止，先保留现场。--yyl
test ! -e /opt/xiaoya-voice
test ! -e /etc/xiaoya-voice/config.json
test ! -e /var/lib/xiaoya-voice/models/zipvoice

git clone /root/xiaoya-voice-transfer/source.bundle /opt/xiaoya-voice
voice_commit=$(cat /root/xiaoya-voice-transfer/commit.txt)
[[ "$voice_commit" =~ ^[a-f0-9]{40}$ ]]
git -C /opt/xiaoya-voice checkout --detach "$voice_commit"

docker load --input /root/xiaoya-voice-transfer/image.tar.gz
cd /opt/xiaoya-voice
python3 tools/verify_loaded_image.py \
  --manifest /root/xiaoya-voice-transfer/image-manifest.json

# 先确认CentOS7当前内核/Docker能运行镜像，不加载模型。--yyl
docker run --rm --network none --read-only --cpus 1 --memory 512m \
  "xiaoya-voice:$voice_commit" \
  python -c 'import platform,numpy,onnxruntime,sherpa_onnx; print("runtime_python="+platform.python_version())'

install -d -o root -g 10001 -m 750 \
  /etc/xiaoya-voice /var/lib/xiaoya-voice/models/zipvoice
tar -xzf /root/xiaoya-voice-transfer/zipvoice-assets.tar.gz \
  --no-same-owner -C /var/lib/xiaoya-voice/models/zipvoice
chown -R 0:10001 /var/lib/xiaoya-voice/models/zipvoice
find /var/lib/xiaoya-voice/models/zipvoice -type d -exec chmod 750 {} +
find /var/lib/xiaoya-voice/models/zipvoice -type f -exec chmod 640 {} +
install -o root -g 10001 -m 640 \
  /root/xiaoya-voice-transfer/config.json /etc/xiaoya-voice/config.json

bash deploy/release.sh "$voice_commit"
curl --fail --silent --show-error http://127.0.0.1:8793/health \
  | python3 -m json.tool
~~~

sha256sum每项应为OK，镜像校验应为content_verified=true，运行探针应输出runtime_python=3.12.x。最后health.ready应为true。某条命令报错时不继续粘贴后续段落；已有目录的情况不是删除目录重装。

默认预算继续为2个推理线程、2条活动流、2.5CPU/4GiB上限，16GB主机多出的内存不自动变成模型并发。发布脚本核对私有模型/参考摘要、资源和就绪，仅管理本语音项目。

在宝塔“Docker → 容器”刷新可查看xiaoya-voice-voice-1。通过终端创建的容器可能不出现在“编排项目”列表，但容器页应可见；不要再点击新建第二个同名项目。

日志和资源：

~~~bash
docker logs --tail 100 xiaoya-voice-voice-1
docker stats --no-stream xiaoya-voice-voice-1
~~~

若Docker安装或镜像探针失败，应先处理内核/运行时兼容；不关闭seccomp、SELinux或全机防火墙来跳过失败。

## 7. 在国内机器做真实签名测速

CentOS7自带Python可能较旧，下面使用镜像内Python3.12运行现有标准库测速脚本，不在宿主执行需要较新语法的smoke_test.py。

国内宝塔终端执行：

~~~bash
cd /opt/xiaoya-voice
voice_commit=$(cat /root/xiaoya-voice-transfer/commit.txt)
docker run --rm --network host --read-only --cpus 0.5 --memory 128m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount type=bind,src=/etc/xiaoya-voice/config.json,dst=/run/voice/config.json,readonly \
  --mount type=bind,src=/opt/xiaoya-voice/tools/smoke_test.py,dst=/smoke_test.py,readonly \
  "xiaoya-voice:$voice_commit" python /smoke_test.py \
  --config /run/voice/config.json \
  --base-url http://127.0.0.1:8793 \
  --user-id 1900000001 \
  --voice xiaoya-a \
  --text '你好！我是小雅，你的精密制造智能工作伙伴。我可以为你实时查询生产工单、追踪加工进度。今天有什么我可以帮你的？'
~~~

这只运行一个轻量签名客户端，不加载第二份模型。complete应为true；cached全false表示此次未复用。first_pcm_ms是首段到达，complete_ms是完整返回，audio_seconds是音频长度。该环回结果不包含电脑到服务器的网络时间。

再次测新合成时换未使用的测试ID，例如1900000002；同ID/同音色/同文字在缓存有效期内重复会复用。

## 8. 当前电脑打开国内测试页，无需先配域名

Windows电脑新开一个PowerShell。替换下面的国内IP、SSH端口和用户名；CentOS实例常见root或centos，按腾讯云实例的实际登录设置填写：

~~~powershell
ssh -N -T -p 22 -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -L 127.0.0.1:18793:127.0.0.1:8793 root@国内服务器公网IP
~~~

认证成功后通常没有新提示，窗口保持打开。如果账号/端口不是root/22，改成实际值。

再开第二个PowerShell：

~~~powershell
Set-Location 'D:\yl\saiadmin-server\voice_server'
& 'C:\Users\50210\AppData\Local\Programs\Python\Python312\python.exe' .\tools\pc_test.py --config 'D:\yl\saiadmin-server\ysim\runtime\xiaoya-voice.json' --upstream 'http://127.0.0.1:18793' --port 8798
~~~

打开[国内测试页](http://127.0.0.1:8798/)。页脚目标应为http://127.0.0.1:18793，页面应显示12个音色，健康为就绪。

保留[现有线上测试页](http://127.0.0.1:8797/)。两页用相同音色、相同54字示例点击“新合成”，轮流测3～5次并导出记录；核对未复用，再另外比较缓存。国内结果包含SSH开销，原线上结果包含HTTPS/原代理链路，配合第7步区分模型和网络耗时。

教程到这里已能在当前电脑完整试听国内机器。测试页127.0.0.1属于这台电脑，不能在另一台设备上直接复用此地址。

## 9. 配置独立国内域名

示例域名为voice-cn.6xmu.com，尚未替用户创建。现有6xmu.com使用Cloudflare时：

1. Cloudflare → 6xmu.com → DNS → Records → Add record。
2. Type选A；Name填voice-cn；IPv4填腾讯云国内公网IPv4。
3. Proxy status选DNS only（灰云），TTL可用Auto。
4. 保存。在Windows运行Resolve-DnsName voice-cn.6xmu.com，确认解析到国内IP。测试域名不混用旧AAAA记录。

这一步用直连路径测国内服务，依据[Cloudflare记录管理](https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-dns-records/)。

国内公网域名访问按腾讯云接入要求办理；如页面出现备案拦截，按[腾讯云接入备案说明](https://cloud.tencent.com/document/product/243/43878)完成对应接入，再测公网HTTPS。第8步为已授权SSH私有连接测速，不需要先公开网站。

## 10. 在宝塔11.1建站并装证书

先核对腾讯云/宝塔80和443已按第2步放行。

宝塔“软件商店 → 已安装”确认使用Nginx；已有Nginx沿用，不用yum再装另一套。若当前用Apache管理其他网站，先保持原站点，不直接替换Web服务。

宝塔“网站 → PHP项目 → 添加站点”，按表填写：

| 字段 | 内容 |
| --- | --- |
| 域名 | voice-cn.6xmu.com |
| 备注 | 小雅国内语音测试 |
| 根目录 | /www/wwwroot/voice-cn.6xmu.com |
| FTP | 不创建 |
| 数据库 | 不创建 |
| PHP版本 | 纯静态 |

站点只是转发入口，不在网站目录放模型/config.json，也不用PHP或数据库。操作入口见[宝塔创建站点](https://docs.bt.cn/user-guide/site/php/create-web/)。

还没有证书时：点击新站点 → SSL → Let's Encrypt → 申请证书，品牌选择Let's Encrypt、算法可选RSA2048；确认域名已解析且80可达时选文件验证，选中voice-cn.6xmu.com，提交申请，成功后保存并启用。面板也支持DNS验证。入口与字段见[宝塔证书申请说明](https://docs.bt.cn/user-guide/site/php/site-config/ssl/)。

已有腾讯云证书时：点击新站点 → SSL → 当前证书；从腾讯云SSL证书控制台下载该域名的“Nginx”证书，密钥KEY填.key内容，证书PEM填完整证书链.crt内容，点击“保存并启用”。确认浏览器HTTPS证书正常后，再开启“强制HTTPS”。这是[腾讯云的宝塔证书部署步骤](https://cloud.tencent.com/document/product/400/50874)。

不要使用原Cloudflare Origin证书给DNS only直连访问，也不把整个旧线上server配置覆盖到宝塔。

## 11. 站点配置加入流式转发

宝塔“网站 → 新站点 → 配置文件”。先将该站点原配置完整复制到本地备份文本。

在该域名server花括号内、最终闭合大括号之前，加入下面两个location；不要放进已有location、不要再套一个server。保留宝塔已有证书、ACME验证及日志配置。若同名location已存在，先核对，不重复添加。

~~~nginx
# 小雅语音精确路径；保留原签名路径及query，关闭合成缓冲。--yyl
location = /health {
    proxy_pass http://127.0.0.1:8793/health;
    proxy_connect_timeout 3s;
    proxy_read_timeout 5s;
    add_header Cache-Control "no-store" always;
}

location = /v1/stream {
    client_max_body_size 360k;
    proxy_pass http://127.0.0.1:8793;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_cache off;
    proxy_hide_header Cache-Control;
    gzip off;
    proxy_connect_timeout 5s;
    proxy_read_timeout 180s;
    proxy_send_timeout 180s;
    add_header Cache-Control "no-store" always;
    add_header X-Accel-Buffering "no" always;
}
~~~

点击保存后，在宝塔终端核对宝塔Nginx配置。标准路径为：

~~~bash
test -x /www/server/nginx/sbin/nginx
/www/server/nginx/sbin/nginx -t
~~~

看到syntax is ok/test is successful后，在“软件商店 → 已安装 → Nginx → 设置 → 服务”点击“重载配置”。路径不符时从本机Nginx设置确认实际位置，不调用另一套系统Nginx。配置校验失败时先恢复该站点备份，不重启全机。

反向代理入口参考[宝塔官方说明](https://docs.bt.cn/user-guide/site/php/site-config/reverse-proxy)；这里用精确配置保证原项目二进制流的路径与缓冲要求。

浏览器打开https://voice-cn.6xmu.com/health，应看到ready=true。该地址是健康JSON，试听页面仍是本机8798。

停止第8步的国内PC测试进程（对应PowerShell按Ctrl+C），用新HTTPS目标重开：

~~~powershell
Set-Location 'D:\yl\saiadmin-server\voice_server'
& 'C:\Users\50210\AppData\Local\Programs\Python\Python312\python.exe' .\tools\pc_test.py --config 'D:\yl\saiadmin-server\ysim\runtime\xiaoya-voice.json' --upstream 'https://voice-cn.6xmu.com' --port 8798
~~~

刷新8798，页脚应为新HTTPS域名，再做全新合成。签名密钥留在本机代理，不嵌入HTML或对外发布PC管理端口。当前正式业务地址不自动切换。

## 12. 故障与停止

| 现象 | 处理 |
| --- | --- |
| source.bundle或其他文件SHA失败 | 重新传对应文件，先不启动 |
| 镜像运行探针退出、Illegal instruction/系统调用错误 | 核对CPU指令、内核/Docker日志，不以关闭安全隔离绕过 |
| 容器Permission denied | 核对root:10001、目录750/文件640；SELinux开启时按下面局部标签处理 |
| 镜像Healthy前503 | 等首次模型预热；超时查看容器日志 |
| HTTPS 502 | 先在宝塔终端curl环回/health，再核对Nginx代理 |
| HTTPS证书错误 | 核对新域名及完整公共证书链，不关闭证书校验 |
| 合成403 | 核对私有配置签名值及系统时间；浏览器直接无签名POST被拒绝属预期 |
| 合成429 | 已有两路活动请求，等请求完成/取消后重试 |
| SSH隧道Address already in use | 检查本机18793占用，不停其他未知进程 |
| 健康正常但听到的是旧服务 | 看8798页脚目标、PowerShell启动参数和实际SSH目标IP |

只有SELinux处于Enforcing、Docker启用SELinux标签且日志明确显示绑定目录访问被拒绝时，在这两个项目目录加容器标签，不关闭SELinux：

~~~bash
getenforce
yum install -y policycoreutils-python
semanage fcontext -a -t container_file_t '/etc/xiaoya-voice(/.*)?'
semanage fcontext -a -t container_file_t '/var/lib/xiaoya-voice/models/zipvoice(/.*)?'
restorecon -Rv /etc/xiaoya-voice /var/lib/xiaoya-voice/models/zipvoice
~~~

若已有同路径标签规则，先查看现有记录再修改，不叠加未知规则。随后通过原发布脚本重试本项目。

标签持久化方式参见[Red Hat SELinux指南](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/7/pdf/selinux_users_and_administrators_guide/red_hat_enterprise_linux-7-selinux_users_and_administrators_guide-en-us.pdf)。

需要停止国内模型时，仅在宝塔“Docker → 容器”中停止xiaoya-voice-voice-1，或执行docker stop xiaoya-voice-voice-1；恢复用docker start xiaoya-voice-voice-1。不要删除卷、模型和配置。宿主Docker保持启动时，容器restart=unless-stopped可在正常主机重启后拉起，手动停止的容器需要手动恢复。

测试记录至少包括：实际CPU型号/内核/Docker版本、音色、文字、新合成或缓存、首段、完整、音频时长及CPU/内存峰值。国内部署能减少多少网络等待，以该机器实测为准。
