import sys,gc,weakref,json,ctypes
from pathlib import Path
from pymysql.connections import Connection
from matrixone._local_infile import install_guard

def rss():
 for x in Path('/proc/self/status').read_text().splitlines():
  if x.startswith('VmRSS:'):return int(x.split()[1])*1024
mode=sys.argv[1]; guarded=mode.startswith('guard'); disabled=mode.endswith('disabled'); gc.collect(); gc.disable(); start=rss(); refs=[]
for i in range(100):
 c=Connection(defer_connect=True,ssl_disabled=disabled)
 if guarded: install_guard(c)
 refs.append(weakref.ref(c)); c._force_close(); del c
before={'rss':rss(),'alive':sum(x() is not None for x in refs)}; assert not guarded or before['alive']==0, before; n=gc.collect(); after={'rss':rss(),'alive':sum(x() is not None for x in refs)}; libc=ctypes.CDLL(None);libc.malloc_trim(0)
print(json.dumps({'mode':mode,'n':100,'start_rss':start,'before_gc':before,'gc_collected':n,'after_gc':after,'after_trim_rss':rss()}))
