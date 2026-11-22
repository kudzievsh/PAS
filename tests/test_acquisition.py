import subprocess
import pytest
from src import ingestion


def test_permanent_http_error_is_not_retried(monkeypatch):
    calls=[]
    def bad(*args,**kwargs):
        calls.append(args);return subprocess.CompletedProcess([],22,'bad query','curl: (22) error: 400')
    monkeypatch.setattr(ingestion.subprocess,'run',bad)
    with pytest.raises(RuntimeError):ingestion.fetch('https://example.test',{})
    assert len(calls)==1


def test_temporary_error_is_retried(monkeypatch):
    calls=[]
    def flaky(*args,**kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([],22,'','curl: error: 429') if len(calls)==1 else subprocess.CompletedProcess([],0,'[]','')
    monkeypatch.setattr(ingestion.subprocess,'run',flaky)
    monkeypatch.setattr(ingestion.time,'sleep',lambda _:None)
    assert ingestion.fetch('https://example.test',{})=='[]'
    assert len(calls)==2
