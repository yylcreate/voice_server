"""生成源码镜像的可校验CI制品；不接触模型或运行时私有配置。--yyl"""
import hashlib,json,os,pathlib,subprocess

commit=os.environ['GITHUB_SHA'];tag='xiaoya-voice:'+commit
directory=pathlib.Path('.ci-artifact');directory.mkdir(exist_ok=True)
archive=directory/'image.tar.gz'
with archive.open('wb')as output:
    producer=subprocess.Popen(['docker','save',tag],stdout=subprocess.PIPE)
    try:
        subprocess.run(['gzip','-1'],stdin=producer.stdout,stdout=output,check=True)
    finally:
        producer.stdout.close()
    if producer.wait()!=0:raise RuntimeError('Docker image export failed')
# 原check_output(...,text=True)；沿用Python3.6也支持的文本输出参数。--yyl
image=json.loads(subprocess.check_output(['docker','image','inspect',tag],universal_newlines=True))[0]
assert image['Config']['Labels']['org.opencontainers.image.revision']==commit
digest=hashlib.sha256()
with archive.open('rb')as source:
    for chunk in iter(lambda:source.read(1024*1024),b''):digest.update(chunk)
fields=['User','Env','Cmd','Entrypoint','WorkingDir','Labels','Healthcheck','ExposedPorts','Volumes','StopSignal','Shell']
manifest={'commit':commit,'image_tag':tag,'image_id':image['Id'],'archive':'image.tar.gz',
          'rootfs_diff_ids':image['RootFS']['Layers'],'runtime_config':{key:image['Config'].get(key)for key in fields},
          'os':image['Os'],'architecture':image['Architecture'],
          'archive_sha256':digest.hexdigest(),'archive_bytes':archive.stat().st_size,'private_assets_included':False}
(directory/'image-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
print(json.dumps(manifest))
