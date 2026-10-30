import importlib.util,json,time,threading,gc,ctypes,sys,tracemalloc,collections
from pathlib import Path
from types import SimpleNamespace
import pyarrow as pa
spec=importlib.util.spec_from_file_location('soak',Path('clients/python/tests/analytics/run_soak.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Mallinfo(ctypes.Structure):
 _fields_=[(n,ctypes.c_size_t) for n in ('arena','ordblks','smblks','hblks','hblkhd','usmblks','fsmblks','uordblks','fordblks','keepcost')]
libc=ctypes.CDLL(None);libc.mallinfo2.restype=Mallinfo
samples=[];snaps=[];stop=threading.Event();started=time.monotonic();tracemalloc.start(1)
def sample():
 while not stop.wait(3):
  mi=libc.mallinfo2();t=time.monotonic()-started
  samples.append({'seconds':round(t,2),'rss':m.memory('VmRSS'),'malloc_live':mi.uordblks,'malloc_free':mi.fordblks,'malloc_mmaps':mi.hblkhd,'arrow':pa.total_allocated_bytes()})
  if (not snaps and t>=32) or (len(snaps)==1 and t>=105):
   objs=collections.Counter(type(o).__module__+'.'+type(o).__qualname__ for o in gc.get_objects() if isinstance(type(o).__module__,str) and isinstance(type(o).__qualname__,str))
   snaps.append((tracemalloc.take_snapshot(),objs,t))
t=threading.Thread(target=sample,daemon=True);t.start();m.memory_pressure=lambda *args:{'skipped':True,'reason':'profiling diagnostic; not acceptance'};error=None
try:m.main(SimpleNamespace(staging_dir=sys.argv[1],output='qa-evidence/profile-raw.json',warmup_seconds=30,seconds=90,workers=8,rows=1000000))
except BaseException as e:error={'type':type(e).__name__,'message':str(e)}
finally:stop.set();t.join()
before=m.memory('VmRSS');collected=gc.collect();aftergc=m.memory('VmRSS');pa.default_memory_pool().release_unused();arrow=m.memory('VmRSS');libc.malloc_trim(0);trim=m.memory('VmRSS')
r={'scope':'instrumented diagnostic, S07 skipped, not a gate result','error':error,'samples':samples,'after_soak_rss':before,'extra_gc_collected':collected,'after_extra_gc_rss':aftergc,'after_arrow_release_rss':arrow,'after_malloc_trim_rss':trim,'snapshot_count':len(snaps)}
if len(snaps)==2:
 a,b=snaps;r['snapshot_times']=[a[2],b[2]];r['python_allocations']=[str(x) for x in b[0].compare_to(a[0],'lineno')[:35]];r['object_growth']=(b[1]-a[1]).most_common(35)
Path('qa-evidence/profile.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps({'scope':r['scope'],'error':error,'snapshot_count':len(snaps),'rss_before':before,'rss_after_gc':aftergc,'rss_after_trim':trim}))

if len(snaps)!=2 or error and error['type']!='AssertionError': raise SystemExit('diagnostic did not capture the intended load')
