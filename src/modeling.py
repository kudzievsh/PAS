"""Seven direct horizons, expanding-window validation and a separate final test."""
from datetime import timedelta
import json
import math
import os
import pickle
import uuid

os.environ.setdefault('OMP_NUM_THREADS','2')
os.environ.setdefault('OPENBLAS_NUM_THREADS','2')
import numpy as np
import pandas as pd
from psycopg2.extras import Json,execute_values
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder,StandardScaler
from sqlalchemy import create_engine,text
from threadpoolctl import threadpool_limits
from src.common import ROOT,SETTINGS,db,dump_json

BASELINE='SeasonalNaive7'
RIDGE='RidgeDirect7'
NUMERIC=['lag0','lag1','lag6','lag7','lag13','lag27','mean7','mean28','std7','std28',
         'temp_min_lag7','temp_max_lag7','precip_lag7','annual_sin','annual_cos','trend']
CATEGORICAL=['category_id','district_id','target_dow','target_month']


def metrics(actual,pred):
    actual=np.asarray(actual,dtype=float);pred=np.asarray(pred,dtype=float)
    err=actual-pred;denom=actual.sum()
    return {'mae':float(np.abs(err).mean()),'rmse':float(np.sqrt(np.mean(err**2))),
            'wape':float(np.abs(err).sum()/denom*100) if denom>0 else None,'n':len(actual)}


def make_features(frame):
    df=frame.sort_values(['category_id','district_id','request_date']).copy()
    group=df.groupby(['category_id','district_id'],sort=False)
    for lag in [0,1,6,7,13,27]:df[f'lag{lag}']=group.request_count.shift(lag)
    for size in [7,28]:
        df[f'mean{size}']=group.request_count.transform(lambda s:s.rolling(size,min_periods=size).mean())
        df[f'std{size}']=group.request_count.transform(lambda s:s.rolling(size,min_periods=size).std())
    for col,source in [('temp_min_lag7','temperature_min'),('temp_max_lag7','temperature_max'),('precip_lag7','precipitation')]:
        df[col]=group[source].shift(7)
    df['trend']=(df.request_date-df.request_date.min()).dt.days/365.25
    df['first_active_date']=group.request_date.transform('min')
    return df


def horizon_data(features,h):
    df=features.copy()
    target=df.request_date+pd.Timedelta(days=h)
    df['target_date']=target
    df['target_dow']=target.dt.dayofweek.astype(str)
    df['target_month']=target.dt.month.astype(str)
    df['annual_sin']=np.sin(target.dt.dayofyear*2*np.pi/365.25)
    df['annual_cos']=np.cos(target.dt.dayofyear*2*np.pi/365.25)
    df['target']=df.groupby(['category_id','district_id'],sort=False).request_count.shift(-h)
    for col in ['category_id','district_id']:df[col]=df[col].astype(str)
    return df


def pipeline():
    numeric=Pipeline([('missing',SimpleImputer(strategy='median',keep_empty_features=True)),('scale',StandardScaler())])
    encoder=ColumnTransformer([('numeric',numeric,NUMERIC),('categories',OneHotEncoder(handle_unknown='ignore'),CATEGORICAL)],sparse_threshold=1.0)
    return Pipeline([('features',encoder),('ridge',Ridge(alpha=20.0,solver='lsqr',tol=1e-4))])


def fit_predict(datasets,origin,save_to=None):
    out=[]
    with threadpool_limits(limits=2):
        for h,data in datasets.items():
            train=data[(data.target_date<=origin)&data.lag27.notna()&data.target.notna()]
            # Training is limited to the last two years to keep memory bounded and adapt to drift.
            train=train[train.request_date>=origin-pd.Timedelta(days=730)]
            future=data[data.request_date==origin].copy()
            model=pipeline()
            model.fit(train[NUMERIC+CATEGORICAL],np.log1p(train.target))
            future['yhat']=np.maximum(0,np.expm1(np.clip(model.predict(future[NUMERIC+CATEGORICAL]),0,15)))
            future['model']=RIDGE
            out.append(future[['target_date','category_id','district_id','target','yhat','model']])
            base=future[['target_date','category_id','district_id','target']].copy()
            # At seven-step forecasting, every seasonal baseline value is already observed at the origin.
            lag=7-h
            reference=data.copy()
            reference['base']=reference.groupby(['category_id','district_id'],sort=False).request_count.shift(lag)
            base['yhat']=reference.loc[reference.request_date==origin,'base'].to_numpy()
            base['model']=BASELINE;out.append(base)
            if save_to:
                save_to.mkdir(parents=True,exist_ok=True)
                with (save_to/f'horizon-{h}.pkl').open('wb') as f:pickle.dump(model,f)
    result=pd.concat(out,ignore_index=True)
    result['category_id']=result.category_id.astype(int);result['district_id']=result.district_id.astype(int)
    return result


def evaluate(frame,split,fold,origin,run_id):
    rows=[]
    for model,part in frame.groupby('model'):
        for level in ['district_category','city_daily']:
            vals=part if level=='district_category' else part.groupby('target_date',as_index=False)[['target','yhat']].sum()
            score=metrics(vals.target,vals.yhat)
            rows.append((run_id,split,fold,model,level,score['mae'],score['rmse'],score['wape'],score['n'],origin.date(),part.target_date.min().date(),part.target_date.max().date()))
    with db() as conn,conn.cursor() as cur:
        execute_values(cur,'INSERT INTO mart.model_metric(run_id,split,fold,model,level,mae,rmse,wape,n,train_end,target_start,target_end) VALUES %s',rows)
        data=[(run_id,split,fold,origin.date(),r.target_date.date(),r.category_id,r.district_id,r.model,int(r.target),float(r.yhat)) for r in frame.itertuples()]
        execute_values(cur,'INSERT INTO mart.backtest(run_id,split,fold,forecast_origin,target_date,category_id,district_id,model,actual,yhat) VALUES %s',data,page_size=5000)
    return rows


def run_models():
    engine=create_engine(os.environ['DATABASE_URL'])
    with engine.connect() as c:frame=pd.read_sql_query(text('SELECT * FROM mart.daily_requests ORDER BY category_id,district_id,request_date'),c,parse_dates=['request_date'])
    engine.dispose()
    if frame.empty:raise ValueError('No mart data')
    origin=frame.request_date.max();first=frame.request_date.min();horizon=SETTINGS['forecast_horizon']
    if horizon!=7:raise ValueError('This model implementation supports exactly seven horizons')
    test_days=SETTINGS['test_days'];folds=SETTINGS['validation_folds']
    validation_end=origin-pd.Timedelta(days=test_days)
    validation_start=validation_end-pd.Timedelta(days=folds*7-1)
    if (validation_start-first).days<SETTINGS['minimum_training_days']:raise ValueError('Insufficient history for temporal validation')
    run=str(uuid.uuid4())
    params={'ridge_alpha':20.0,'target_transform':'log1p','max_training_days':730,
       'numeric_features':NUMERIC,'categorical_features':CATEGORICAL,
       'weather_availability_lag_days':7,'seed':SETTINGS['random_seed'],
       'scope':SETTINGS['categories'],'selection':'minimum mean validation WAPE at district-category level'}
    with db() as conn,conn.cursor() as cur:
        cur.execute('INSERT INTO mart.model_run(run_id,cutoff_date,horizon,parameters,validation_start,validation_end,test_start,test_end) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(run,origin.date(),7,Json(params),validation_start.date(),validation_end.date(),(validation_end+pd.Timedelta(days=1)).date(),origin.date()))
    features=make_features(frame)
    datasets={h:horizon_data(features,h) for h in range(1,8)}
    # Feature frames cover all dates, but fitting selects labels ending no later than each forecast origin.
    all_scores=[]
    for fold in range(folds):
        cutoff=validation_start-pd.Timedelta(days=1)+pd.Timedelta(days=7*fold)
        pred=fit_predict(datasets,cutoff)
        all_scores+=evaluate(pred,'validation',fold+1,cutoff,run)
        print('Validation fold',fold+1,'origin',cutoff.date(),flush=True)
    val=pd.DataFrame(all_scores,columns=['run','split','fold','model','level','mae','rmse','wape','n','train_end','target_start','target_end'])
    selected=val[val.level=='district_category'].groupby('model').wape.mean().idxmin()
    # The final test does not influence model choice or hyperparameters.
    for fold in range(test_days//7):
        cutoff=validation_end+pd.Timedelta(days=fold*7)
        pred=fit_predict(datasets,cutoff)
        all_scores+=evaluate(pred,'test',fold+1,cutoff,run)
        print('Final test fold',fold+1,'origin',cutoff.date(),flush=True)
    with db() as conn,conn.cursor() as cur:
        cur.execute('UPDATE mart.model_run SET selected_model=%s,selection_reason=%s WHERE run_id=%s',(selected,'Lowest mean validation WAPE; final test excluded from selection',run))
    future=fit_predict(datasets,origin,ROOT/'data/models'/run)
    with db() as conn,conn.cursor() as cur:
        rows=[(run,origin.date(),r.target_date.date(),r.category_id,r.district_id,r.model,float(r.yhat)) for r in future.itertuples()]
        execute_values(cur,'INSERT INTO mart.prediction(run_id,forecast_origin,target_date,category_id,district_id,model,yhat) VALUES %s',rows,page_size=5000)
        cur.execute("UPDATE mart.model_run SET status='SUCCESS' WHERE run_id=%s",(run,))
    summary=pd.DataFrame(all_scores,columns=val.columns).groupby(['split','model','level'])[['mae','rmse','wape']].mean().reset_index()
    dump_json(ROOT/'data/exports/model_summary.json',{'run_id':run,'origin':origin.date(),'selected_model':selected,
        'metrics':summary.to_dict('records'),'parameters':params,'date_from':first.date(),
        'date_to':origin.date(),'rows':len(frame),'series':frame[['category_id','district_id']].drop_duplicates().shape[0]})
    future.to_csv(ROOT/'data/exports/forecast.csv',index=False)
    print('Forecast complete',run,'selected',selected,flush=True)
    return run
