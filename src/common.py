import json
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import yaml
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
SETTINGS = yaml.safe_load((ROOT / 'config/settings.yaml').read_text())


@contextmanager
def db(admin=False, database=None):
    url = os.environ['ADMIN_DATABASE_URL' if admin else 'DATABASE_URL']
    conn = psycopg2.connect(url, application_name='PAS', connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql, params=None):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def dump_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
