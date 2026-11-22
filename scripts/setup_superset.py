from pathlib import Path
import os
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
os.environ['SUPERSET_HOME']=str(ROOT/'.state/superset')
os.environ['SUPERSET_CONFIG_PATH']=str(ROOT/'config/superset_config.py')
os.environ['FLASK_APP']='superset'
for args in [('db','upgrade'),('init',)]:
    subprocess.run([str(ROOT/'.venv/bin/superset'),*args],check=True,cwd=ROOT)
from superset.app import create_app
app=create_app()
with app.app_context():
    from superset import security_manager
    if not security_manager.find_user(username='admin'):
        security_manager.add_user('admin','Шота','Кудзиев','admin@pas.local',security_manager.find_role('Admin'),os.environ['SUPERSET_ADMIN_PASSWORD'])
    from src.dashboard import create_dashboards
    create_dashboards()
print('Superset initialized, dashboards ready')
