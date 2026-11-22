"""Start/stop local UI without a continuously running terminal window."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.state'


def healthy():
    try:
        with urlopen('http://127.0.0.1:8088/health',timeout=2) as r:return r.status==200
    except Exception:return False


def start(open_browser=True):
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    subprocess.run([sys.executable,str(ROOT/'scripts/manage_db.py'),'start'],check=True)
    subprocess.run([sys.executable,str(ROOT/'scripts/manage_db.py'),'migrate'],check=True)
    if not healthy():
        env=os.environ.copy();env.update(SUPERSET_HOME=str(STATE/'superset'),SUPERSET_CONFIG_PATH=str(ROOT/'config/superset_config.py'),FLASK_APP='superset')
        log=(STATE/'superset-server.log').open('a')
        proc=subprocess.Popen([str(ROOT/'.venv/bin/gunicorn'),'--bind','127.0.0.1:8088','--workers','1','--threads','2','--timeout','180',
          '--pid',str(STATE/'superset.pid'),'--no-control-socket','superset.app:create_app()'],
          cwd=ROOT,env=env,stdout=log,stderr=log,start_new_session=True)
        for _ in range(90):
            if healthy():break
            if proc.poll() is not None:raise RuntimeError('Superset startup failed. See .state/superset-server.log')
            time.sleep(1)
        else:raise RuntimeError('Superset startup timeout')
    print('\nАдрес: http://127.0.0.1:8088/superset/dashboard/pas-demand/')
    print('Логин: admin')
    print('Пароль:',os.environ['SUPERSET_ADMIN_PASSWORD'])
    print('Данные входа хранятся только в локальном файле .env.\n')
    if open_browser:subprocess.run(['/usr/bin/open','http://127.0.0.1:8088/superset/dashboard/pas-demand/'],check=True)


def stop():
    pid=STATE/'superset.pid'
    if pid.exists():
        value=int(pid.read_text().strip())
        # Confirm the PID belongs to this project's Gunicorn process before signalling it.
        import psutil
        try:
            proc=psutil.Process(value)
            if str(ROOT) not in ' '.join(proc.cmdline()):raise RuntimeError('PID file points to another process')
            proc.terminate();proc.wait(timeout=30)
        except psutil.NoSuchProcess:pass
        pid.unlink(missing_ok=True)
    subprocess.run([sys.executable,str(ROOT/'scripts/manage_db.py'),'stop'],check=True)
    print('Superset и база проекта выключены.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop']);p.add_argument('--no-browser',action='store_true');a=p.parse_args()
    start(not a.no_browser) if a.action=='start' else stop()
