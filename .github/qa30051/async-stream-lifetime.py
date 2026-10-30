import asyncio,gc,json,logging,weakref
from sqlalchemy.ext.asyncio import create_async_engine
from matrixone import AsyncClient
logging.getLogger('matrixone').disabled=True
async def main():
 e=create_async_engine('mysql+aiomysql://root:111@127.0.0.1:6003/mo_analytics_test',pool_size=1,max_overflow=0)
 c=AsyncClient.from_engine(e,query_timeout=10,sql_log_mode='off'); refs=[];gc.collect();gc.disable()
 for i in range(20):
  async with c.analytics.query('SELECT 1 AS n UNION ALL SELECT 2 AS n',batch_size=1) as s:
   refs.append(weakref.ref(s._driver));assert (await s.__anext__())['n'].to_pylist()==[1]
  del s
 await asyncio.sleep(0)
 alive=sum(r() is not None for r in refs);item=next((r() for r in refs if r() is not None),None); k=[type(v).__module__+'.'+type(v).__name__ for v in gc.get_referrers(item)] if item else [];del item
 n=gc.collect();after=sum(r() is not None for r in refs)
 print(json.dumps({'total':len(refs),'alive_before_gc':alive,'after_gc':after,'referrers':k,'checkedout':e.pool.checkedout(),'collected':n}))
 assert len(refs)==20 and alive==0 and e.pool.checkedout()==0
 await c.disconnect()
asyncio.run(main())
