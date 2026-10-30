import gc,json,logging,weakref
import pyarrow as pa
from matrixone import Client
logging.getLogger('matrixone').disabled=True
c=Client(host='127.0.0.1',port=6003,user='root',password='111',database='mo_analytics_test',pool_size=1,max_overflow=1,query_timeout=10)
gc.collect();gc.disable();refs={};stats={}
for kind in ('stream','reject'):
 r=[]
 for i in range(20):
  try:
   if kind=='stream':
    with c.analytics.query('SELECT 1 AS n UNION ALL SELECT 2 AS n',batch_size=1) as stream:
     r.append(weakref.ref(stream._driver));assert next(stream)['n'].to_pylist()==[1]
   else:
    try:
     with c.analytics.query('SELECT 1000 AS n',schema=pa.schema([('n',pa.int8())])) as stream:
      r.append(weakref.ref(stream._driver));stream.to_arrow()
    except pa.ArrowInvalid:pass
    else:raise AssertionError('overflow accepted')
  finally:
   stream=None
  # No GC collection between iterations.
 refs[kind]=r
 stats[kind]={'total':len(r),'alive_before_gc':sum(x() is not None for x in r)}
 sample=next((x() for x in r if x() is not None),None)
 if sample is not None:
  stats[kind]['referrers']=[type(x).__module__+'.'+type(x).__name__ for x in gc.get_referrers(sample)]
 del sample
stats['checkedout']=c._engine.pool.checkedout();stats['gc_collected']=gc.collect()
for kind,r in refs.items():stats[kind]['alive_after_gc']=sum(x() is not None for x in r)
c.disconnect();print(json.dumps(stats,indent=2));assert all(stats[k]['total']==20 and stats[k]['alive_before_gc']==0 for k in ('stream','reject'));assert stats['checkedout']==0
