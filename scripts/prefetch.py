"""Download resumable public-source snapshots before loading PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import date,datetime,timedelta
import gzip
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.common import SETTINGS,dump_json
from src.ingestion import NYC,FIELDS,fetch,digest,months


def partition(first,last):
    scope=digest(SETTINGS['categories'])
    folder=ROOT/'data/cache/nyc';folder.mkdir(parents=True,exist_ok=True)
    target=folder/f'{scope[:12]}-{first}-{last}.jsonl.gz'
    manifest=target.with_suffix('.manifest.json')
    if target.exists() and manifest.exists():
        print('Cached',first,last,flush=True);return
    cats=','.join("'"+c.replace("'","''")+"'" for c in SETTINGS['categories'])
    where=f"created_date >= '{first}T00:00:00' AND created_date < '{last+timedelta(days=1)}T00:00:00' AND complaint_type in ({cats})"
    count=json.loads(fetch(NYC,{'$select':'count(*) AS n','$where':where}));expected=int(count[0]['n'])
    tmp=target.with_suffix('.part');cursor=None;total=0
    try:
        with gzip.open(tmp,'wt',encoding='utf-8') as f:
            while True:
                clause=where
                if cursor:
                    stamp,key=cursor
                    clause+=f" AND (created_date > '{stamp}' OR (created_date = '{stamp}' AND unique_key > '{key}'))"
                rows=json.loads(fetch(NYC,{'$select':FIELDS,'$where':clause,'$order':'created_date,unique_key','$limit':SETTINGS['page_size']}))
                if not isinstance(rows,list):raise ValueError('Source returned an invalid record array')
                for row in rows:f.write(json.dumps(row,separators=(',',':'))+'\n')
                total+=len(rows)
                print(first,'downloaded',total,'/',expected,flush=True)
                if len(rows)<SETTINGS['page_size']:break
                cursor=(rows[-1]['created_date'],str(rows[-1]['unique_key']).replace("'","''"))
        if total!=expected:raise ValueError(f'Partition count changed {first}: {total} != {expected}; retry the partition')
        tmp.replace(target)
        dump_json(manifest,{'source':NYC,'date_from':first,'date_to':last,'scope_hash':scope,
           'rows':total,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
           'retrieved_at':datetime.now(ZoneInfo('UTC')).isoformat(),'fields':FIELDS.split(',')})
    except BaseException:
        tmp.unlink(missing_ok=True);raise


if __name__=='__main__':
    start=date.fromisoformat(SETTINGS['start_date'])
    end=datetime.now(ZoneInfo('America/New_York')).date()-timedelta(days=SETTINGS['publication_lag_days'])
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(partition,a,b) for a,b in months(start,end)]
        for f in as_completed(futures):f.result()
    print('All source snapshots downloaded and count-checked',flush=True)
