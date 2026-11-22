"""Integration checks use rolled-back fixture records and never alter source data."""
from datetime import date
import uuid
import psycopg2
import os
import pytest
from src.common import db,query
from src import ingestion


def test_upsert_and_observed_history_are_idempotent():
    conn=psycopg2.connect(os.environ['DATABASE_URL']);load=str(uuid.uuid4())
    record={'unique_key':'9999999999999998','created_date':'2023-01-01T00:00:00.000',
      'complaint_type':'HEAT/HOT WATER','community_board':'01 MANHATTAN','borough':'MANHATTAN','agency':'HPD','status':'Open'}
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO ops.load_log(load_id,source_name,load_mode,date_from,date_to,status) VALUES(%s,'integration_test','test','2023-01-01','2023-01-01','STARTED')",(load,))
        assert ingestion.save_requests([record,record],load,conn)==(1,0,0)
        with conn.cursor() as cur:cur.execute('DROP TABLE batch_request')
        assert ingestion.save_requests([record],load,conn)==(0,0,0)
        with conn.cursor() as cur:cur.execute('DROP TABLE batch_request')
        record['status']='Closed'
        assert ingestion.save_requests([record],load,conn)==(0,1,0)
        with conn.cursor() as cur:
            cur.execute('SELECT count(*),count(*) FILTER(WHERE valid_to IS NULL) FROM core.request_history WHERE unique_key=%s',(record['unique_key'],))
            assert cur.fetchone()==(2,1)
            cur.execute('SELECT count(*) FROM raw.service_request WHERE unique_key=%s',(record['unique_key'],))
            assert cur.fetchone()==(1,)
    finally:conn.rollback();conn.close()


def test_failure_does_not_advance_watermark(monkeypatch):
    before=query("SELECT complete_through FROM ops.watermark WHERE source_name='nyc_311'")
    def unavailable(*args,**kwargs):raise RuntimeError('Controlled integration test: source temporarily unavailable')
    monkeypatch.setattr(ingestion,'fetch',unavailable)
    with pytest.raises(RuntimeError,match='Controlled integration test'):
        ingestion.nyc_load(date(2023,1,1),date(2023,1,1),mode='fault_injection',resume=False)
    after=query("SELECT complete_through FROM ops.watermark WHERE source_name='nyc_311'")
    assert before==after
    result=query("SELECT status,error_message FROM ops.load_log WHERE load_mode='fault_injection' ORDER BY started_at DESC LIMIT 1")[0]
    assert result['status']=='FAILED'
    assert result['error_message'].startswith('Controlled integration test')
