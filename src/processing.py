import uuid
from src.common import db,query
from psycopg2.extras import Json


def process():
    with db() as conn,conn.cursor() as cur:
        cur.execute('INSERT INTO core.category(category_name) SELECT DISTINCT category_name FROM staging.v_requests ON CONFLICT(category_name) DO NOTHING')
        cur.execute('INSERT INTO core.district(community_board,borough) SELECT DISTINCT community_board,borough FROM staging.v_requests ON CONFLICT(community_board,borough) DO NOTHING')
        cur.execute('''INSERT INTO core.service_request
            SELECT s.unique_key,s.created_at,s.request_date,c.category_id,d.district_id,s.agency,s.status,s.load_id
            FROM staging.v_requests s JOIN core.category c USING(category_name)
            JOIN core.district d USING(community_board,borough)
            ON CONFLICT(unique_key) DO UPDATE SET category_id=excluded.category_id,district_id=excluded.district_id,
              agency=excluded.agency,status=excluded.status,source_load_id=excluded.source_load_id
            WHERE (core.service_request.category_id,core.service_request.district_id,core.service_request.agency,core.service_request.status)
            IS DISTINCT FROM (excluded.category_id,excluded.district_id,excluded.agency,excluded.status)''')
        cur.execute('''INSERT INTO core.weather SELECT observation_date,station_id,temperature_min,temperature_max,precipitation,snow,load_id
            FROM staging.v_weather WHERE (temperature_min IS NULL OR temperature_max IS NULL OR temperature_min<=temperature_max)
            AND (precipitation IS NULL OR precipitation>=0) AND (snow IS NULL OR snow>=0)
            ON CONFLICT(observation_date) DO UPDATE SET temperature_min=excluded.temperature_min,temperature_max=excluded.temperature_max,
              precipitation=excluded.precipitation,snow=excluded.snow,source_load_id=excluded.source_load_id''')
        # Rebuild in one transaction. Readers see either the previous or the complete new mart.
        cur.execute('DELETE FROM mart.daily_requests')
        cur.execute('''INSERT INTO mart.daily_requests
            WITH bounds AS (SELECT min(request_date) AS first_day,
              (SELECT complete_through FROM ops.watermark WHERE source_name='nyc_311') AS last_day FROM core.service_request),
            pairs AS (SELECT DISTINCT category_id,district_id FROM core.service_request),
            daily AS (SELECT request_date,category_id,district_id,count(*)::integer AS n FROM core.service_request GROUP BY 1,2,3)
            SELECT calendar.day::date,p.category_id,p.district_id,coalesce(a.n,0),
              w.temperature_min,w.temperature_max,w.precipitation,w.snow
            FROM bounds b CROSS JOIN LATERAL generate_series(b.first_day,b.last_day,interval '1 day') calendar(day)
            CROSS JOIN pairs p LEFT JOIN daily a ON a.request_date=calendar.day::date AND a.category_id=p.category_id AND a.district_id=p.district_id
            LEFT JOIN core.weather w ON w.observation_date=calendar.day::date''')
    print('Storage layers and daily mart refreshed',flush=True)


def check_quality():
    checks=[
      ('Обязательные поля обращений','completeness','ERROR',"SELECT count(*) FROM core.service_request WHERE unique_key IS NULL OR request_date IS NULL OR category_id IS NULL OR district_id IS NULL",'= 0'),
      ('Уникальность ключей обращений','uniqueness','ERROR',"SELECT count(*)-count(DISTINCT unique_key) FROM core.service_request",'= 0'),
      ('Полнота переноса raw в core','completeness','ERROR',"SELECT (SELECT count(*) FROM raw.service_request)-(SELECT count(*) FROM core.service_request)",'= 0'),
      ('Числовой тип идентификатора источника','type','ERROR',"SELECT count(*) FROM raw.service_request WHERE unique_key !~ '^[0-9]+$'",'= 0'),
      ('Допустимые значения статуса','validity','WARN',"SELECT count(*) FROM core.service_request WHERE status NOT IN ('Open','Closed','Assigned','Pending','Started','In Progress','Unspecified')",'= 0'),
      ('Отрицательные суточные количества','range','ERROR',"SELECT count(*) FROM mart.daily_requests WHERE request_count<0",'= 0'),
      ('Температура и осадки в допустимом диапазоне','range','ERROR',"SELECT count(*) FROM core.weather WHERE temperature_min < -60 OR temperature_max > 60 OR temperature_min>temperature_max OR precipitation<0 OR snow<0",'= 0'),
      ('Ссылочная целостность витрины','referential_integrity','ERROR',"SELECT count(*) FROM mart.daily_requests m LEFT JOIN core.category c USING(category_id) LEFT JOIN core.district d USING(district_id) WHERE c.category_id IS NULL OR d.district_id IS NULL",'= 0'),
      ('Сверка суммы витрины и обращений','reconciliation','ERROR',"SELECT abs((SELECT sum(request_count) FROM mart.daily_requests)-(SELECT count(*) FROM core.service_request))",'= 0'),
      ('Покрытие календаря погодой','completeness','WARN',"SELECT count(*) FROM (SELECT DISTINCT request_date FROM mart.daily_requests WHERE temperature_min IS NULL OR temperature_max IS NULL) x",'= 0'),
      ('Неопределённая территориальная привязка','validity','WARN',"SELECT count(*) FROM core.service_request r JOIN core.district d USING(district_id) WHERE d.borough='UNKNOWN' OR d.community_board !~ '^[0-9]{2} (BRONX|BROOKLYN|MANHATTAN|QUEENS|STATEN ISLAND)$'",'= 0'),
      ('Пропуски в календаре витрины','completeness','ERROR',"SELECT (max(request_date)-min(request_date)+1)-count(DISTINCT request_date) FROM mart.daily_requests",'= 0'),
      ('Одна текущая версия историзации','uniqueness','ERROR',"SELECT count(*) FROM (SELECT unique_key FROM core.request_history WHERE valid_to IS NULL GROUP BY unique_key HAVING count(*)<>1) x",'= 0'),
      ('Согласованность текущей версии истории','reconciliation','ERROR',"SELECT count(*) FROM raw.service_request r LEFT JOIN core.request_history h ON h.unique_key=r.unique_key AND h.valid_to IS NULL WHERE h.history_id IS NULL OR h.source_hash<>r.source_hash",'= 0'),
      ('Актуальность обращений','freshness','WARN',"SELECT greatest(0,((current_timestamp AT TIME ZONE 'America/New_York')::date-(SELECT max(request_date) FROM core.service_request))-5)",'age <= 5 days'),
      ('Актуальность погоды','freshness','WARN',"SELECT greatest(0,((current_timestamp AT TIME ZONE 'America/New_York')::date-(SELECT max(observation_date) FROM core.weather))-7)",'age <= 7 days'),
      ('Актуальность каждой категории обращений','freshness','WARN',"SELECT count(*) FROM ops.v_category_freshness WHERE status='WARN'",'no category older than 14 days'),
    ]
    run=str(uuid.uuid4()); failures=[]
    # Save every result, including failures, before raising the publication gate.
    with db() as conn,conn.cursor() as cur:
        for name,kind,severity,sql,rule in checks:
            cur.execute(sql);v=cur.fetchone()[0]
            status='PASS' if v is not None and v==0 else ('FAIL' if severity=='ERROR' else 'WARN')
            cur.execute('INSERT INTO ops.quality_result(run_id,check_name,check_type,severity,observed_value,expected_rule,status,details) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(run,name,kind,severity,v,rule,status,Json({'sql':sql})))
            if status=='FAIL':failures.append(name)
    print('Quality checks',run,'failures:',failures,flush=True)
    if failures:raise RuntimeError('Quality gate failed: '+', '.join(failures))
    return run
