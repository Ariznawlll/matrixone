import time,json,pymysql,sys
for attempt in range(150):
 try:
  c=pymysql.connect(host='127.0.0.1',port=6003,user='root',password='111',connect_timeout=3,read_timeout=10,write_timeout=10,autocommit=True)
  with c.cursor() as cur:
   cur.execute('SELECT git_version()');version=cur.fetchone()[0]
   assert version=='a548509',version
   cur.execute('CREATE DATABASE IF NOT EXISTS mo_analytics_test');cur.execute('USE mo_analytics_test');cur.execute('SHOW TABLES');assert not cur.fetchall()
  c.close();print(json.dumps({'version':version,'tables':0,'attempt':attempt+1}));sys.exit(0)
 except Exception:time.sleep(2)
raise SystemExit('MO readiness timed out')
