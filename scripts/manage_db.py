"""Project-local PostgreSQL instance. Does not use the user's existing databases."""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PG = Path(os.environ.get('POSTGRES_BIN', '/Applications/Postgres.app/Contents/Versions/latest/bin'))
STATE = ROOT / '.state'
DATA = STATE / 'postgres'
PORT = 55432


def run(*args, **kwargs):
    return subprocess.run([str(PG / args[0]), *args[1:]], check=True, **kwargs)


def start():
    STATE.mkdir(exist_ok=True)
    (ROOT / '.env').touch(mode=0o600, exist_ok=True)
    if not (ROOT / '.env').read_text():
        admin, app, reader, secret = [secrets.token_hex(24) for _ in range(4)]
        (ROOT / '.env').write_text(
            f'ADMIN_DATABASE_URL=postgresql://pas_admin:{admin}@127.0.0.1:{PORT}/postgres\n'
            f'DATABASE_URL=postgresql://pas_app:{app}@127.0.0.1:{PORT}/pas_analytics\n'
            f'SUPERSET_DATABASE_URL=postgresql+psycopg2://pas_app:{app}@127.0.0.1:{PORT}/pas_superset\n'
            f'READER_DATABASE_URL=postgresql+psycopg2://pas_reader:{reader}@127.0.0.1:{PORT}/pas_analytics\n'
            f'PAS_APP_PASSWORD={app}\nPAS_READER_PASSWORD={reader}\n'
            f'SUPERSET_SECRET_KEY={secret}\nSUPERSET_ADMIN_PASSWORD={secrets.token_urlsafe(15)}\n'
        )
        (ROOT / '.env').chmod(0o600)
    sys.path.insert(0,str(ROOT))
    from dotenv import load_dotenv
    import psycopg2
    from psycopg2 import sql
    from urllib.parse import urlparse
    load_dotenv(ROOT / '.env')
    if not (DATA / 'PG_VERSION').exists():
        pw = STATE / 'init-password'
        pw.write_text(urlparse(os.environ['ADMIN_DATABASE_URL']).password)
        pw.chmod(0o600)
        try:
            run('initdb', '-D', str(DATA), '-U', 'pas_admin', '--auth=scram-sha-256', '--pwfile', str(pw), '--encoding=UTF8','--locale=C')
        finally:
            pw.unlink(missing_ok=True)
        with (DATA / 'postgresql.conf').open('a') as f:
            f.write(f"\nport={PORT}\nlisten_addresses='127.0.0.1'\nshared_buffers='128MB'\nwork_mem='8MB'\nmaintenance_work_mem='64MB'\nmax_connections=25\nunix_socket_directories='/tmp'\n")
    running=False
    try:
        probe=psycopg2.connect(os.environ['ADMIN_DATABASE_URL'],connect_timeout=2)
        probe.close();running=True
    except psycopg2.OperationalError:pass
    if not running:
        run('pg_ctl','-D',str(DATA),'-l',str(STATE / 'postgres.log'),'-w','start')
    conn=psycopg2.connect(os.environ['ADMIN_DATABASE_URL']);conn.autocommit=True
    with conn.cursor() as cur:
        for role, env in [('pas_app','PAS_APP_PASSWORD'),('pas_reader','PAS_READER_PASSWORD')]:
            cur.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,))
            if not cur.fetchone():
                cur.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD %s').format(sql.Identifier(role)),(os.environ[env],))
        for name in ['pas_analytics','pas_superset']:
            cur.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,))
            if not cur.fetchone():cur.execute(sql.SQL('CREATE DATABASE {} OWNER pas_app').format(sql.Identifier(name)))
    conn.close()
    print('Project PostgreSQL ready at 127.0.0.1:55432')


def migrate():
    sys.path.insert(0,str(ROOT))
    from src.common import db
    with db() as conn,conn.cursor() as cur:
        cur.execute('CREATE SCHEMA IF NOT EXISTS meta; CREATE TABLE IF NOT EXISTS meta.schema_migrations(version text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz DEFAULT now())')
        for path in sorted((ROOT / 'sql').glob('*.sql')):
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            cur.execute('SELECT checksum FROM meta.schema_migrations WHERE version=%s',(path.name,))
            old=cur.fetchone()
            if old:
                if old[0]!=digest:raise RuntimeError('Applied migration changed: '+path.name)
                continue
            cur.execute(path.read_text())
            cur.execute('INSERT INTO meta.schema_migrations(version,checksum) VALUES(%s,%s)',(path.name,digest))
    # Only the administrator grants access to the read-only BI role.
    import psycopg2
    url=os.environ['ADMIN_DATABASE_URL'].rsplit('/',1)[0]+'/pas_analytics'
    with psycopg2.connect(url) as conn,conn.cursor() as cur:
        cur.execute('GRANT CONNECT ON DATABASE pas_analytics TO pas_reader')
        cur.execute('GRANT USAGE ON SCHEMA mart,ops,meta TO pas_reader')
        cur.execute('GRANT SELECT ON ALL TABLES IN SCHEMA mart,ops,meta TO pas_reader')
        cur.execute('ALTER DEFAULT PRIVILEGES FOR ROLE pas_app IN SCHEMA mart,ops,meta GRANT SELECT ON TABLES TO pas_reader')
    print('Schema migrations complete')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','migrate','stop']);a=p.parse_args()
    if a.action=='start':start()
    elif a.action=='migrate':migrate()
    else:run('pg_ctl','-D',str(DATA),'-m','fast','-w','stop')
