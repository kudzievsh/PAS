"""Measure a repeat computation; excludes PostgreSQL and web/browser processes."""
from pathlib import Path
import resource
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.pipeline import run
from src.common import dump_json
started=time.monotonic()
run(skip_download=True)
rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
# macOS reports bytes; Linux reports KiB.
peak_mib=rss/(1024*1024) if sys.platform=='darwin' else rss/1024
dump_json(ROOT/'data/exports/performance.json',{'scope':'Python processing, quality and two models; PostgreSQL, Superset and browser excluded',
   'seconds':time.monotonic()-started,'peak_rss_mib':peak_mib,'platform':sys.platform})
print('Performance measurement saved')
