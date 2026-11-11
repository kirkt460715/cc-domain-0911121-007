import json,hashlib,urllib.request
from source import start,ROOT
server,base=start()
try:
    import gzip
    raw=urllib.request.urlopen(base+'/usage').read();lines=gzip.decompress(raw).splitlines()
    assert len(lines)==4 and json.loads(lines[-1])['totals']['requests']=='3.75'
    try:gzip.decompress(urllib.request.urlopen(base+'/usage?cut=1').read());raise AssertionError('应发现截断')
    except EOFError:pass
    assert b'4' in gzip.decompress(urllib.request.urlopen(base+'/usage?total=1').read())
finally:
    server.shutdown();server.server_close()
print('本地输入协议与故障样本检查通过')
