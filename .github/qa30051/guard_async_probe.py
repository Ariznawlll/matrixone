import gc,weakref,json,sys
from aiomysql.connection import Connection
from matrixone._local_infile import install_guard
import matrixone._local_infile as module
assert '/clients/python/matrixone/' in module.__file__, module.__file__
gc.collect();gc.disable();refs=[]
for i in range(100):
 c=Connection();install_guard(c);refs.append(weakref.ref(c));c.close();del c
alive=sum(x() is not None for x in refs)
print(json.dumps({'kind':'aiomysql','connections':100,'alive_before_gc':alive,'guard_module':module.__file__}))
assert alive==0
