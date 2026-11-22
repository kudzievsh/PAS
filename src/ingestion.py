"""Partitioned, retryable acquisition. NYC JSON and NOAA CSV remain distinct formats."""
import calendar
import csv
from datetime import date,datetime,timedelta
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from zoneinfo import ZoneInfo

from psycopg2.extras import Json,execute_values
from src.common import ROOT,SETTINGS,db,query

NYC='https://data.cityofnewyork.us/resource/erm2-nwe9.json'
NOAA='https://www.ncei.noaa.gov/access/services/data/v1'
FIELDS='unique_key,created_date,complaint_type,community_board,borough,agency,status,closed_date,resolution_action_updated_date'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def fetch(url,params):
    # macOS curl uses the system trust store, including institution-managed CAs.
    # Certificate checking is always enabled. Arguments are never passed through a shell.
    args=['/usr/bin/curl','--silent','--show-error','--fail-with-body','--location',
          '--connect-timeout','15','--max-time',str(SETTINGS['request_timeout']),
          '--get',url,'--user-agent','PAS-Academic-Project/1.0']
    for k,v in params.items():args += ['--data-urlencode',f'{k}={v}']
    if os.getenv('SOCRATA_APP_TOKEN') and url==NYC:args+=['-H','X-App-Token: '+os.environ['SOCRATA_APP_TOKEN']]
    for attempt in range(SETTINGS['retry_attempts']):
        result=subprocess.run(args,capture_output=True,text=True)
        if result.returncode==0:return result.stdout
        # Authentication, syntax errors and forbidden requests will not improve with retries.
        terminal=any(f'error: {s}' in result.stderr for s in ['400','401','403','404'])
        if terminal or attempt+1==SETTINGS['retry_attempts']:
            raise RuntimeError(f'Source HTTP failure: {result.stderr[-350:]} {result.stdout[:350]}')
        time.sleep(min(2**attempt,16))


def start_load(source,mode,start,end):
    load=str(uuid.uuid4())
    with db() as conn,conn.cursor() as cur:
        cur.execute('INSERT INTO ops.load_log(load_id,source_name,load_mode,date_from,date_to,status,parameters) VALUES(%s,%s,%s,%s,%s,\'STARTED\',%s)',
                    (load,source,mode,start,end,Json({'categories':SETTINGS['categories'],'station':SETTINGS['station']})))
    return load


def finish(load,status,error=None):
    with db() as conn,conn.cursor() as cur:
        cur.execute('UPDATE ops.load_log SET status=%s,finished_at=now(),error_message=%s WHERE load_id=%s',(status,error,load))


def observe_schema(source,fields,load):
    h=digest(fields)
    with db() as conn,conn.cursor() as cur:
        cur.execute('INSERT INTO meta.source_schema(source_name,schema_hash,fields,load_id) VALUES(%s,%s,%s,%s) ON CONFLICT(source_name,schema_hash) DO NOTHING',(source,h,Json(fields),load))


def check_nyc_schema(load):
    columns=json.loads(fetch('https://data.cityofnewyork.us/api/views/erm2-nwe9/columns.json',{}))
    if isinstance(columns,dict):columns=columns.get('columns',columns.get('data',[]))
    fields=[{'name':c['fieldName'],'type':c.get('dataTypeName')} for c in columns if isinstance(c,dict) and 'fieldName' in c]
    if not set(FIELDS.split(',')).issubset({f['name'] for f in fields}):raise ValueError('Required NYC source columns disappeared; review source schema')
    observe_schema('nyc_311',fields,load)


def save_requests(records,load,conn):
    """One transaction stores raw records, observed history, rejects and counters."""
    accepted={}; rejected=[]
    for row in records:
        try:
            key=str(row['unique_key']).strip(); created=datetime.fromisoformat(row['created_date'])
            if not key or not row.get('complaint_type'):raise ValueError('Missing key/category')
            if created.tzinfo:raise ValueError('Expected NYC local floating timestamp')
            if row['complaint_type'] not in SETTINGS['categories']:raise ValueError('Category outside configured scope')
            accepted[key]=(key,created,Json(row),digest(row),load)
        except (KeyError,ValueError,TypeError) as e:rejected.append((load,row.get('unique_key'),str(e),Json(row)))
    with conn.cursor() as cur:
        cur.execute('CREATE TEMP TABLE batch_request (LIKE raw.service_request INCLUDING DEFAULTS) ON COMMIT DROP')
        if accepted:
            execute_values(cur,'INSERT INTO batch_request(unique_key,created_at,payload,source_hash,load_id) VALUES %s',list(accepted.values()),page_size=10000)
            cur.execute('SELECT count(*) FILTER(WHERE r.unique_key IS NULL), count(*) FILTER(WHERE r.unique_key IS NOT NULL AND r.source_hash<>b.source_hash) FROM batch_request b LEFT JOIN raw.service_request r USING(unique_key)')
            inserted,updated=cur.fetchone()
            cur.execute('UPDATE core.request_history h SET valid_to=statement_timestamp() FROM batch_request b WHERE h.unique_key=b.unique_key AND h.valid_to IS NULL AND h.source_hash<>b.source_hash')
            cur.execute('INSERT INTO core.request_history(unique_key,source_hash,payload,load_id) SELECT b.unique_key,b.source_hash,b.payload,b.load_id FROM batch_request b LEFT JOIN core.request_history h ON h.unique_key=b.unique_key AND h.valid_to IS NULL WHERE h.history_id IS NULL')
            cur.execute('INSERT INTO raw.service_request(unique_key,created_at,payload,source_hash,load_id) SELECT unique_key,created_at,payload,source_hash,load_id FROM batch_request ON CONFLICT(unique_key) DO UPDATE SET payload=excluded.payload,source_hash=excluded.source_hash,load_id=excluded.load_id,last_seen_at=now() WHERE raw.service_request.source_hash<>excluded.source_hash')
        else:inserted=updated=0
        if rejected:execute_values(cur,'INSERT INTO ops.rejected_record(load_id,record_key,reason,payload) VALUES %s',rejected)
        cur.execute('UPDATE ops.load_log SET rows_received=rows_received+%s,rows_inserted=rows_inserted+%s,rows_updated=rows_updated+%s,rows_rejected=rows_rejected+%s WHERE load_id=%s',(len(records),inserted,updated,len(rejected),load))
    return inserted,updated,len(rejected)


def months(start,end):
    while start<=end:
        last=min(date(start.year,start.month,calendar.monthrange(start.year,start.month)[1]),end)
        yield start,last
        start=last+timedelta(days=1)


def nyc_load(start,end,mode='incremental',resume=True):
    load=start_load('nyc_311',mode,start,end)
    scope=digest(SETTINGS['categories'])
    cats=','.join("'"+c.replace("'","''")+"'" for c in SETTINGS['categories'])
    try:
        check_nyc_schema(load)
        for first,last in months(start,end):
            if resume and mode=='historical' and query('SELECT 1 FROM ops.loaded_partition WHERE source_name=\'nyc_311\' AND period_start=%s AND period_end=%s AND scope_hash=%s',(first,last,scope)):
                print('NYC cached partition',first,last,flush=True);continue
            cache=ROOT/'data/cache/nyc'/f'{scope[:12]}-{first}-{last}.jsonl.gz'
            manifest=cache.with_suffix('.manifest.json')
            if mode=='historical' and os.getenv('PAS_WAIT_FOR_CACHE')=='1':
                deadline=time.monotonic()+1800
                while not manifest.exists() and time.monotonic()<deadline:time.sleep(2)
            if mode=='historical' and cache.exists() and manifest.exists():
                info=json.loads(manifest.read_text())
                if hashlib.sha256(cache.read_bytes()).hexdigest()!=info['sha256']:raise ValueError('Cached source checksum mismatch')
                total=0;batch=[]
                with gzip.open(cache,'rt',encoding='utf-8') as f:
                    for line in f:
                        batch.append(json.loads(line))
                        if len(batch)>=SETTINGS['page_size']:
                            with db() as conn:save_requests(batch,load,conn)
                            total+=len(batch);batch=[]
                    if batch:
                        with db() as conn:save_requests(batch,load,conn)
                        total+=len(batch)
                if total!=info['rows']:raise ValueError('Cached source count mismatch')
                with db() as conn,conn.cursor() as cur:
                    cur.execute('INSERT INTO ops.loaded_partition(source_name,period_start,period_end,scope_hash,load_id,row_count) VALUES(\'nyc_311\',%s,%s,%s,%s,%s) ON CONFLICT(source_name,period_start,period_end,scope_hash) DO UPDATE SET load_id=excluded.load_id,row_count=excluded.row_count,completed_at=now()', (first,last,scope,load,total))
                print('NYC loaded verified snapshot',first,total,flush=True)
                continue
            until=last+timedelta(days=1)
            where=f"created_date >= '{first}T00:00:00' AND created_date < '{until}T00:00:00' AND complaint_type in ({cats})"
            cursor=None;total=0
            while True:
                clause=where
                if cursor:
                    stamp,key=cursor
                    clause+=f" AND (created_date > '{stamp}' OR (created_date = '{stamp}' AND unique_key > '{key}'))"
                rows=json.loads(fetch(NYC,{'$select':FIELDS,'$where':clause,'$order':'created_date,unique_key','$limit':SETTINGS['page_size']}))
                if not isinstance(rows,list):raise ValueError('NYC response is not a record array')
                if not rows:break
                with db() as conn:save_requests(rows,load,conn)
                total+=len(rows)
                cursor=(rows[-1]['created_date'],str(rows[-1]['unique_key']).replace("'","''"))
                print('NYC',first,last,'rows',total,flush=True)
                if len(rows)<SETTINGS['page_size']:break
            with db() as conn,conn.cursor() as cur:
                cur.execute('INSERT INTO ops.loaded_partition(source_name,period_start,period_end,scope_hash,load_id,row_count) VALUES(\'nyc_311\',%s,%s,%s,%s,%s) ON CONFLICT(source_name,period_start,period_end,scope_hash) DO UPDATE SET load_id=excluded.load_id,row_count=excluded.row_count,completed_at=now()', (first,last,scope,load,total))
        with db() as conn,conn.cursor() as cur:
            cur.execute('INSERT INTO ops.watermark(source_name,complete_through,load_id) VALUES(\'nyc_311\',%s,%s) ON CONFLICT(source_name) DO UPDATE SET complete_through=greatest(ops.watermark.complete_through,excluded.complete_through),load_id=excluded.load_id,updated_at=now()',(end,load))
        finish(load,'SUCCESS')
    except BaseException as e:
        finish(load,'FAILED',str(e)[:2000]);raise
    return load


def weather_load(start,end,mode='incremental'):
    load=start_load('noaa_daily',mode,start,end)
    try:
        body=fetch(NOAA,{'dataset':'daily-summaries','stations':SETTINGS['station'],
               'startDate':start,'endDate':end,'format':'csv','units':'metric',
               'dataTypes':'TMIN,TMAX,PRCP,SNOW','includeAttributes':'false'})
        reader=csv.DictReader(io.StringIO(body))
        if not reader.fieldnames or not {'STATION','DATE'}.issubset(reader.fieldnames):raise ValueError('NOAA CSV columns missing')
        observe_schema('noaa_daily',reader.fieldnames,load)
        rows=list(reader)
        with db() as conn,conn.cursor() as cur:
            inserted=updated=rejected=0
            for row in rows:
                try:
                    day=date.fromisoformat(row['DATE'])
                    if row['STATION']!=SETTINGS['station'] or not start<=day<=end:raise ValueError('Station/date outside query')
                    for k in ['TMIN','TMAX','PRCP','SNOW']:
                        if row.get(k):float(row[k])
                    cur.execute('SELECT source_hash FROM raw.weather WHERE station_id=%s AND observation_date=%s',(row['STATION'],day));old=cur.fetchone();h=digest(row)
                    inserted+=old is None;updated+=old is not None and old[0]!=h
                    cur.execute('INSERT INTO raw.weather(station_id,observation_date,payload,source_hash,load_id) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(station_id,observation_date) DO UPDATE SET payload=excluded.payload,source_hash=excluded.source_hash,load_id=excluded.load_id,fetched_at=now() WHERE raw.weather.source_hash<>excluded.source_hash',(row['STATION'],day,Json(row),h,load))
                except (ValueError,KeyError) as e:
                    rejected+=1;cur.execute('INSERT INTO ops.rejected_record(load_id,record_key,reason,payload) VALUES(%s,%s,%s,%s)',(load,row.get('DATE'),str(e),Json(row)))
            cur.execute('UPDATE ops.load_log SET rows_received=%s,rows_inserted=%s,rows_updated=%s,rows_rejected=%s WHERE load_id=%s',(len(rows),inserted,updated,rejected,load))
            if not rows:raise ValueError('NOAA returned no rows for the requested range')
            # Track the actual latest observation, not the requested endpoint.
            latest=max(date.fromisoformat(row['DATE']) for row in rows)
            cur.execute('INSERT INTO ops.watermark(source_name,complete_through,load_id) VALUES(\'noaa_daily\',%s,%s) ON CONFLICT(source_name) DO UPDATE SET complete_through=greatest(ops.watermark.complete_through,excluded.complete_through),load_id=excluded.load_id,updated_at=now()',(latest,load))
        finish(load,'SUCCESS');print('NOAA',len(rows),'daily observations',flush=True)
    except BaseException as e:
        finish(load,'FAILED',str(e)[:2000]);raise
    return load


def acquire(historical=False,end=None):
    start=date.fromisoformat(SETTINGS['start_date'])
    end=end or datetime.now(ZoneInfo('America/New_York')).date()-timedelta(days=SETTINGS['publication_lag_days'])
    mode='historical' if historical else 'incremental'
    for source,loader in [('noaa_daily',weather_load),('nyc_311',nyc_load)]:
        begin=start
        if not historical:
            rows=query('SELECT complete_through FROM ops.watermark WHERE source_name=%s',(source,))
            if rows:begin=max(start,rows[0]['complete_through']-timedelta(days=SETTINGS['revision_lookback_days']))
        loader(begin,end,mode=mode)
