import json,pymysql
c=pymysql.connect(host='127.0.0.1',port=6003,user='root',password='111',database='mo_analytics_test',autocommit=True)
with c.cursor() as cur:
 cur.execute('SHOW TABLES');rows=cur.fetchall()
c.close();print(json.dumps({'remaining_tables':len(rows)}));assert not rows
