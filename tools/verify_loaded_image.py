"""核对已验证CI制品加载后的文件层和配置，兼容Docker镜像ID表示差异。--yyl"""
import argparse,json,re,subprocess
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('--manifest',type=Path,required=True);args=parser.parse_args()
manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
commit=manifest['commit']
assert re.fullmatch('[a-f0-9]{40}',commit)and manifest['image_tag']=='xiaoya-voice:'+commit
image=json.loads(subprocess.check_output(['docker','image','inspect',manifest['image_tag']],text=True))[0]
assert image['RootFS']['Layers']==manifest['rootfs_diff_ids'],'Loaded image filesystem mismatch'
assert manifest['runtime_config']and all(image['Config'].get(key)==value for key,value in manifest['runtime_config'].items()),'Loaded image runtime configuration mismatch'
assert image['Os']==manifest['os']and image['Architecture']==manifest['architecture'],'Loaded image platform mismatch'
assert image['Config']['Labels']['org.opencontainers.image.revision']==commit,'Loaded image revision mismatch'
print(json.dumps({'content_verified':True,'commit':commit,'loaded_image_id':image['Id'],
                  'ci_reported_image_id':manifest['image_id'],'rootfs_layers':len(image['RootFS']['Layers'])}))
