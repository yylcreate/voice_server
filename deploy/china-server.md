# 国内服务器部署与对照测试

迁移现有ZipVoice-Distill、原12个音色和合成参数。先通过SSH隧道验证国内机器，不切换正式voice.6xmu.com。--yyl

适用基准：Ubuntu 24.04/26.04、Linux x86_64、4 vCPU、约12GB内存。现有发布脚本要求可见CPU恰好4个、总内存至少11000MiB；其他规格须先适配资源检查。容器限2.5CPU/4GiB，单模型工作线程、2个推理线程、最多2条活动请求；2条接入不代表两条同时实时推理。

“原服务器”指当前运行ZipVoice的AI主机，“国内服务器”指新测试机器。root Bash命令不在Windows PowerShell执行。

## 1. 国内机器准备

先按[Docker官方Ubuntu教程](https://docs.docker.com/engine/install/ubuntu/)安装Docker Engine及Compose插件；已有正常Docker时直接检查版本，不卸载或替换同机Docker。

国内服务器执行：

~~~bash
sudo -i
apt-get update
apt-get install -y git python3 curl tar gzip
docker --version
docker compose version
uname -m
nproc
free -m
~~~

架构应为x86_64。首次测试只需SSH端口可达；模型端口8793保持环回，不开放公网。

## 2. 原服务器导出迁移包

推荐迁移已验证的运行镜像，避免国内机器现场拉Docker Hub、PyPI或模型。应用包不依赖GitHub Actions短期制品。镜像迁移依据[Docker save](https://docs.docker.com/reference/cli/docker/image/save/)与[Docker load](https://docs.docker.com/reference/cli/docker/image/load/)。

SSH登录原服务器，进入root Bash，执行：

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
printf '迁移包目录：%s\n' "$voice_package"
~~~

创建新的root私有目录，不停止原模型。导出以实际运行镜像revision为准，不把只改文档/域名的Git HEAD当成模型版本。

**整个迁移目录包含私有音色、参考文字及签名配置，只通过SSH/SFTP传输，不上传公开Git、网站或公开网盘。** 模型原许可证一并保留。

把输出的整个目录传到国内机器/root下。可以用SFTP，也可以在原服务器执行，替换实际后缀和国内IP：

~~~bash
scp -r /root/xiaoya-voice-cn.替换为实际后缀 root@替换为国内服务器IP:/root/
~~~

两台机器不能互通时，经SFTP先下载到自己的电脑再上传。首次连接核对SSH主机指纹。

## 3. 国内机器安装并启动

国内服务器root Bash执行，第一行改成实际上传目录：

~~~bash
set -Eeuo pipefail
umask 077
voice_package=/root/xiaoya-voice-cn.替换为实际后缀
cd "$voice_package"
sha256sum -c SHA256SUMS

# 本段用于首次安装，已有目录时停止，先保留原部署。--yyl
test ! -e /opt/xiaoya-voice
test ! -e /etc/xiaoya-voice/config.json
test ! -e /var/lib/xiaoya-voice/models/zipvoice

git clone "$voice_package/source.bundle" /opt/xiaoya-voice
voice_commit=$(cat "$voice_package/commit.txt")
[[ "$voice_commit" =~ ^[a-f0-9]{40}$ ]]
git -C /opt/xiaoya-voice checkout --detach "$voice_commit"

docker load --input "$voice_package/image.tar.gz"
cd /opt/xiaoya-voice
python3 tools/verify_loaded_image.py \
  --manifest "$voice_package/image-manifest.json"

install -d -o root -g 10001 -m 750 \
  /var/lib/xiaoya-voice/models/zipvoice /etc/xiaoya-voice
tar -xzf "$voice_package/zipvoice-assets.tar.gz" \
  --no-same-owner -C /var/lib/xiaoya-voice/models/zipvoice
chown -R 0:10001 /var/lib/xiaoya-voice/models/zipvoice
find /var/lib/xiaoya-voice/models/zipvoice -type d -exec chmod 750 {} +
find /var/lib/xiaoya-voice/models/zipvoice -type f -exec chmod 640 {} +
install -o root -g 10001 -m 640 \
  "$voice_package/config.json" /etc/xiaoya-voice/config.json

bash deploy/release.sh "$voice_commit"
curl --fail --silent --show-error http://127.0.0.1:8793/health \
  | python3 -m json.tool
~~~

先校验传输SHA，再核对镜像文件层、运行配置和revision。发布脚本继续检查模型、参考摘要、签名配置与硬件；看到ready为true且runtime.target为amd-4vcpu-12gb再测真实合成。

必须迁移完整模型目录，包括INT8、fp32、vocos、词典、espeak、音色目录及12份参考，不能只复制两个ONNX文件。首次启动需要预热，不等于每次请求耗时。

失败时检查：

~~~bash
docker logs --tail 100 xiaoya-voice-voice-1
docker stats --no-stream xiaoya-voice-voice-1
~~~

发布脚本仅管理xiaoya-voice项目；不执行docker compose down -v或重启同机业务/AI容器。

## 4. 国内服务器先测一段完整语音

~~~bash
cd /opt/xiaoya-voice
python3 tools/smoke_test.py \
  --config /etc/xiaoya-voice/config.json \
  --base-url http://127.0.0.1:8793 \
  --user-id 1900000001 \
  --voice xiaoya-a \
  --text '你好！我是小雅，你的精密制造智能工作伙伴。我可以为你实时查询生产工单、追踪加工进度。今天有什么我可以帮你的？'
~~~

complete应为true；cached全false才是此次未复用合成。first_pcm_ms为首段PCM耗时，complete_ms为完整返回耗时，audio_seconds为音频时长。

同一ID、音色和文字在缓存有效期内复用；重测新合成应换未使用的测试ID。健康200不能代替签名合成验证。服务器环回结果不含用户网络耗时。

## 5. 当前Windows电脑用浏览器对照

新开PowerShell窗口启动SSH隧道，替换国内服务器IP；窗口保持打开：

~~~powershell
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -L 127.0.0.1:18793:127.0.0.1:8793 root@替换为国内服务器IP
~~~

另开PowerShell窗口，启动新版PC测试台：

~~~powershell
Set-Location 'D:\yl\saiadmin-server\voice_server'
& 'C:\Users\50210\AppData\Local\Programs\Python\Python312\python.exe' .\tools\pc_test.py --config 'D:\yl\saiadmin-server\ysim\runtime\xiaoya-voice.json' --upstream 'http://127.0.0.1:18793' --port 8798
~~~

浏览器打开两个页面：

- [现有线上测试台](http://127.0.0.1:8797/)：仍连接https://voice.6xmu.com。
- [国内服务器测试台](http://127.0.0.1:8798/)：通过18793 SSH隧道连接国内机器。

两页都选温柔桃子升级版A和上面的54字完整示例，点击“新合成”。先各测一次，再轮流测3～5次并导出记录，核对cached全false；另行比较“同文复测”。

目标只在启动时指定，网页不能覆盖。远端HTTPS保持证书校验，HTTP仅允许环回目标，跨主机部分由SSH加密。迁移包保留原签名值，因此当前PC的私有配置可继续使用，密钥不用填入网页。

国内结果包含SSH传输，现有线上结果包含HTTPS及原代理链路；配合服务器环回数据判断模型和网络差异。播放启动为浏览器时间戳估算，非扬声器实测。

本教程迁移包包含的是运行镜像对应的旧源码，国内模型不需要新版PC工具；浏览器侧使用当前电脑新版源码即可。在另一台电脑测试时，需要Python 3.12、新版仓库、私有签名值和可读取的音色目录，不需要模型或参考音频。双击HTML不能替代本机签名代理。

## 6. 测试通过后配置国内HTTPS

准备独立测试域名与公共可信证书，例如voice-cn.6xmu.com（待配置示例），暂不切换正式voice.6xmu.com。

按新服务器已有Nginx及证书管理方式安装证书。先备份配置，将仓库deploy/nginx.voice.conf复制为/etc/nginx/snippets/xiaoya-voice-cn.conf，在独立443站点包含：

~~~nginx
server {
    listen 443 ssl;
    server_name voice-cn.6xmu.com;

    ssl_certificate /etc/nginx/ssl/voice-cn/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/voice-cn/privkey.pem;

    include /etc/nginx/snippets/xiaoya-voice-cn.conf;
    location = / { return 302 /health; }
    location = /health { rewrite ^/health$ /voice/health last; }
    location / { return 404; }
}
~~~

证书路径按实际填写；共享443的协议选项应与已有站点一致。通过nginx -t再平滑reload。原/v1/stream路径及query保持，流式缓冲必须关闭。

setup_voice_domain.py绑定当前线上域名和证书路径，不直接用于这个新域名。原Cloudflare Origin证书不作为浏览器直连的公共证书。

新域名正常后，停止国内测试台的前台窗口，改用：

~~~powershell
Set-Location 'D:\yl\saiadmin-server\voice_server'
& 'C:\Users\50210\AppData\Local\Programs\Python\Python312\python.exe' .\tools\pc_test.py --config 'D:\yl\saiadmin-server\ysim\runtime\xiaoya-voice.json' --upstream 'https://voice-cn.6xmu.com' --port 8798
~~~

8798此时测量国内HTTPS路径。验证通过后再决定业务后台切换；后台基础URL仅含协议、主机及可选端口，不带/voice或/v1/stream。

## 7. 保留测试结果

记录CPU型号、资源、实际镜像revision；按原A/阿宝/其他音色分别记录新合成与缓存。保留首段、完整返回、音频时长、播放空档及CPU/内存峰值。

服务器环回、国内SSH、国内HTTPS各自记录；再测1条、2条不同新文本同时请求及第三条429，不由一次短句推断大量并发容量。

国内部署主要改变网络路径及代理耗时，同规格CPU仍需完成原模型计算，能提升多少以实测为准。
