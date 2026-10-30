import ctypes,gc,importlib.util,json,os,sys,threading,time,traceback,types
from pathlib import Path
from types import SimpleNamespace
import pyarrow as pa
op,mode,stage=sys.argv[1:]; assert op in ('stream','reject') and mode in ('preferred','disabled')
assert 'MALLOC_ARENA_MAX' not in os.environ
source=Path('clients/python/tests/analytics/run_soak.py').read_text()
a="rng.choice(['stream', 'stream', 'file', 'upload', 'reject'])"; assert a in source;source=source.replace(a,repr(op))
b="    return Client(\n";assert b in source;source=source.replace(b,"    return Client(\n        ssl_mode="+repr(mode)+",\n",1)
m=types.ModuleType('isolated_soak');exec(compile(source,'isolated_soak.py','exec'),m.__dict__)
class Malloc(ctypes.Structure):
 _fields_=[(x,ctypes.c_size_t) for x in ('arena','ordblks','smblks','hblks','hblkhd','usmblks','fsmblks','uordblks','fordblks','keepcost')]
libc=ctypes.CDLL(None);libc.mallinfo2.restype=Malloc
stop=threading.Event(); samples=[];started=time.monotonic()
def sample():
 while not stop.wait(1):
  v=libc.mallinfo2(); samples.append({'seconds':time.monotonic()-started,'rss':m.memory('VmRSS'),'malloc_live':v.uordblks,'malloc_free':v.fordblks,'arrow':pa.total_allocated_bytes()})
t=threading.Thread(target=sample,daemon=True);t.start();m.memory_pressure=lambda *a:{'skipped':True,'reason':'attribution probe, not acceptance'};error=None
path=Path('qa-evidence')/f'attribution-{op}-{mode}-raw.json'
try:m.main(SimpleNamespace(staging_dir=stage,output=str(path),warmup_seconds=10,seconds=30,workers=8,rows=1000000))
except BaseException as e:error={'type':type(e).__name__,'message':str(e)}
finally:stop.set();t.join()
pre=m.memory('VmRSS');collected=gc.collect();post=m.memory('VmRSS');pa.default_memory_pool().release_unused();after_arrow=m.memory('VmRSS');libc.malloc_trim(0);after_trim=m.memory('VmRSS')
report={'scope':'short isolated operation diagnostic; not a gate test','operation':op,'tls_mode':mode,'error':error,'samples':samples,'rss_after':pre,'after_gc':post,'after_arrow':after_arrow,'after_trim':after_trim,'gc_collected':collected}
if path.exists():
 j=json.loads(path.read_text());s=j['soak'];report.update({'counts':s['counts'],'warmup_counts':s['warmup_counts'],'errors':s['errors'],'delta_mib':s['peak_rss_mib']-s['before']['rss']/1048576})
Path(f'qa-evidence/attribution-{op}-{mode}.json').write_text(json.dumps(report,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='samples'},ensure_ascii=False));assert path.exists() and not report['errors']
