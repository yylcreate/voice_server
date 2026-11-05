#!/usr/bin/env bash
# 按明确提交发布独立语音容器，失败恢复旧镜像；不管理其他项目。--yyl
set -Eeuo pipefail
umask 077

fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || fail 'Run this deployment as root.'
[[ ( $# -eq 1 || $# -eq 2 ) && $1 =~ ^[a-f0-9]{40}$ ]] || fail 'Provide the reviewed full Git commit and optional staged config.'
commit=$1
live_config=/etc/xiaoya-voice/config.json
candidate_config=${2:-$live_config}
candidate_config=$(realpath -e -- "$candidate_config")
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$root"
[[ $(git rev-parse HEAD) == "$commit" ]] || fail 'Checkout is not the reviewed commit.'
[[ -z $(git status --porcelain --untracked-files=no) ]] || fail 'Tracked source has local modifications.'
[[ -f $live_config && ! -L $live_config && -f $candidate_config && -d /var/lib/xiaoya-voice/models/zipvoice ]] || fail 'Private config/model assets are missing.'
command -v docker >/dev/null
command -v curl >/dev/null
command -v python3 >/dev/null
docker compose version >/dev/null
exec 9>/run/lock/xiaoya-voice-deploy.lock
flock -n 9 || fail 'Another voice deployment is running.'

export XIAOYA_VOICE_IMAGE="xiaoya-voice:$commit"
export XIAOYA_VOICE_COMMIT="$commit"
compose=(docker compose --project-name xiaoya-voice --file "$root/compose.yaml")
old_container=$(docker ps -aq --filter label=com.docker.compose.project=xiaoya-voice --filter label=com.docker.compose.service=voice)
[[ $(printf '%s\n' "$old_container" | sed '/^$/d' | wc -l) -le 1 ]] || fail 'Multiple existing voice containers require inspection.'
old_image='';old_running=false
if [[ -n $old_container ]]; then
    old_image=$(docker inspect --format '{{.Config.Image}}' "$old_container")
    old_running=$(docker inspect --format '{{.State.Running}}' "$old_container")
fi

# 先使用已核验并加载的CI镜像；没有该提交镜像时才本机构建。--yyl
# 原无条件 docker build --build-arg "VCS_REF=$commit" --tag "$XIAOYA_VOICE_IMAGE" .
if ! docker image inspect "$XIAOYA_VOICE_IMAGE" >/dev/null 2>&1; then
    docker build --build-arg "VCS_REF=$commit" --tag "$XIAOYA_VOICE_IMAGE" .
fi
[[ $(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$XIAOYA_VOICE_IMAGE") == "$commit" ]] || fail 'Image revision mismatch.'
docker run --rm --network none --read-only --cpus 2.5 --memory 4g --pids-limit 128 \
    --mount "type=bind,src=$candidate_config,dst=/run/voice/config.json,readonly" \
    --mount type=bind,src=/var/lib/xiaoya-voice/models/zipvoice,dst=/models/zipvoice,readonly \
    "$XIAOYA_VOICE_IMAGE" python deploy/check_target.py --config /run/voice/config.json --require-target-hardware

if [[ $old_running == true ]]; then
    curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8793/health \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ready"] and d["active"]==0, "Voice service must be healthy and idle before switching"'
fi

release_dir="/var/lib/xiaoya-voice/releases/$(date -u +%Y%m%dT%H%M%SZ)-${commit:0:12}"
install -d -m 700 "$release_dir"
if [[ $candidate_config != "$live_config" ]]; then
    install -m 600 -- "$live_config" "$release_dir/config.before.json"
fi

replace_config() {
    local staged
    staged=$(mktemp /etc/xiaoya-voice/.config-release.XXXXXXXX)
    install -o root -g 10001 -m 640 -- "$1" "$staged" && mv -T -- "$staged" "$live_config"
}

switched=false
rollback() {
    local status=$?
    trap - ERR
    if [[ $switched == true ]]; then
        if [[ -f $release_dir/config.before.json ]]; then
            replace_config "$release_dir/config.before.json" || fail 'Restore the saved voice configuration before restarting.'
        fi
        if [[ -n $old_image && $old_running == true ]]; then
            export XIAOYA_VOICE_IMAGE="$old_image"
            "${compose[@]}" up -d --no-deps voice || true
            "${compose[@]}" exec -T voice python deploy/check_ready.py --timeout 180 || true
        else
            "${compose[@]}" stop voice || true
        fi
    fi
    printf 'Voice deployment failed (exit %s); inspect retained container logs and private assets.\n' "$status" >&2
    exit "$status"
}
trap rollback ERR
switched=true
if [[ $candidate_config != "$live_config" ]]; then
    replace_config "$candidate_config"
fi
"${compose[@]}" up -d --no-deps voice
"${compose[@]}" exec -T voice python deploy/check_ready.py --timeout 180

container=$("${compose[@]}" ps -q voice)
docker inspect --format '{{.Id}} {{.Image}}' "$container" > "$release_dir/container.txt"
printf '%s\n' "$commit" > "$release_dir/commit.txt"
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8793/health > "$release_dir/health.json"
trap - ERR
printf 'Voice service ready: commit=%s, receipt=%s\n' "$commit" "$release_dir"
