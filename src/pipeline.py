import argparse
from datetime import date,datetime
import fcntl
import json
from pathlib import Path
import traceback
import uuid

from psycopg2.extras import Json
from src.common import ROOT,db
from src.ingestion import acquire
from src.processing import process,check_quality
from src.modeling import run_models


def run(historical=False,skip_download=False,skip_model=False,end=None):
    lockfile=ROOT/'.state/pipeline.lock';lockfile.parent.mkdir(exist_ok=True)
    with lockfile.open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another pipeline run is already active')
        run_id=str(uuid.uuid4());steps={}
        with db() as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO meta.pipeline_run(run_id,status) VALUES(%s,'STARTED')",(run_id,))
        try:
            jobs=[]
            if not skip_download:jobs.append(('ingestion',lambda:acquire(historical,end)))
            jobs += [('processing',process),('quality',check_quality)]
            if not skip_model:jobs.append(('forecast',run_models))
            for name,fn in jobs:
                print('Running',name,flush=True);steps[name]={'started_at':datetime.now().astimezone().isoformat()}
                result=fn();steps[name].update(status='SUCCESS',result=str(result),finished_at=datetime.now().astimezone().isoformat())
                with db() as conn,conn.cursor() as cur:
                    cur.execute('UPDATE meta.pipeline_run SET steps=%s WHERE run_id=%s',(Json(steps),run_id))
            with db() as conn,conn.cursor() as cur:
                cur.execute("UPDATE meta.pipeline_run SET status='SUCCESS',finished_at=now(),steps=%s WHERE run_id=%s",(Json(steps),run_id))
            print('Pipeline complete',run_id,flush=True)
        except BaseException as e:
            with db() as conn,conn.cursor() as cur:
                cur.execute("UPDATE meta.pipeline_run SET status='FAILED',finished_at=now(),steps=%s,error_message=%s WHERE run_id=%s",(Json(steps),str(e)[:3000],run_id))
            raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description='Complete NYC 311 forecast pipeline')
    p.add_argument('--historical',action='store_true');p.add_argument('--skip-download',action='store_true');p.add_argument('--skip-model',action='store_true')
    p.add_argument('--end',type=date.fromisoformat)
    a=p.parse_args();run(a.historical,a.skip_download,a.skip_model,a.end)
