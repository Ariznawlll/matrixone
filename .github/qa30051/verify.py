import json,sys
j=json.load(open(sys.argv[1]));s=j['soak'];delta=s['peak_rss_mib']-s['before']['rss']/1048576
assert j['passed'] and s['seconds']==90 and s['warmup_seconds']==30 and s['workers']==8
assert not s['errors'] and not s['external_leaks'] and not s['owned_tables_left']
assert s['after']['checkedout']==0 and s['after']['pinned']==0 and s['arrow_live_bytes']==0 and delta<64
assert s['after']['fds']<=s['before']['fds']+16
m=j['memory_pressure'];assert m[0]['mode']=='streamed' and m[0]['rows']==1000000 and m[0]['checksum']==499999500000 and m[0]['error'] is None
assert m[1]['mode']=='buffered' and any(v in m[1]['error']['classes'] for v in ('MemoryError','TimeoutExpired'))
print(json.dumps({'operations':sum(s['counts'].values()),'counts':s['counts'],'warmup':sum(s['warmup_counts'].values()),'rss_delta_mib':round(delta,2),'fd_peak':s['peak_fds'],'checkedout':s['after']['checkedout'],'stream_rows':m[0]['rows'],'stream_checksum':m[0]['checksum'],'buffered_error_classes':m[1]['error']['classes']}))
