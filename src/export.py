"""Export measured results and publication figures from PostgreSQL."""
import os
os.environ.setdefault('MPLCONFIGDIR',str(__import__('pathlib').Path(__file__).resolve().parents[1]/'.state/matplotlib'))
import json
from datetime import date
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from sqlalchemy import create_engine,text
from src.common import ROOT,query,dump_json


def export():
    folder=ROOT/'docs/figures';folder.mkdir(parents=True,exist_ok=True)
    engine=create_engine(os.environ['DATABASE_URL'])
    with engine.connect() as c:
        daily=pd.read_sql_query(text('SELECT * FROM mart.v_daily_requests'),c,parse_dates=['request_date'])
        forecast=pd.read_sql_query(text('SELECT * FROM mart.v_selected_forecast'),c,parse_dates=['target_date'])
        backtest=pd.read_sql_query(text("SELECT b.* FROM mart.backtest b JOIN mart.model_run r USING(run_id) WHERE b.run_id=(SELECT run_id FROM mart.model_run WHERE status='SUCCESS' ORDER BY created_at DESC LIMIT 1) AND split='test'"),c,parse_dates=['target_date'])
    engine.dispose()
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.2,'svg.fonttype':'none'})
    labels={'HEAT/HOT WATER':'Отопление и горячая вода','Street Condition':'Состояние улиц','Water System':'Водоснабжение'}
    monthly=daily.groupby([pd.Grouper(key='request_date',freq='MS'),'category_name']).request_count.sum().unstack()
    fig,ax=plt.subplots(figsize=(9,4.4))
    for col in monthly.columns:ax.plot(monthly.index,monthly[col],label=labels[col],linewidth=1.7)
    ax.set(ylabel='Обращения в месяц',xlabel='Месяц')
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6));ax.xaxis.set_major_formatter(mdates.DateFormatter('%m.%Y'))
    ax.legend(frameon=False,fontsize=9);fig.tight_layout()
    for ext in ['png','svg']:fig.savefig(folder/f'monthly_categories.{ext}',dpi=180,bbox_inches='tight')
    plt.close(fig)

    actual=daily.groupby('request_date').request_count.sum();future=forecast.groupby('target_date').yhat.sum()
    fig,ax=plt.subplots(figsize=(9,4.2))
    recent=actual.loc[actual.index>=actual.index.max()-pd.Timedelta(days=60)]
    ax.plot(recent.index,recent.values,color='#245b88',label='Факт',linewidth=1.6)
    ax.plot(future.index,future.values,color='#b54c2f',marker='o',label='Прогноз SeasonalNaive7',linewidth=1.7)
    ax.axvspan(future.index.min(),future.index.max(),color='#f4dece',alpha=.4)
    ax.set(ylabel='Обращения в сутки',xlabel='Дата')
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1));ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
    ax.legend(frameon=False,fontsize=10);fig.tight_layout()
    for ext in ['png','svg']:fig.savefig(folder/f'forecast.{ext}',dpi=180,bbox_inches='tight')
    plt.close(fig)

    fig,ax=plt.subplots(figsize=(9,4.2))
    for name,part in backtest.groupby('model'):
        vals=part.groupby('target_date').yhat.sum();ax.plot(vals.index,vals.values,label=name,linewidth=1.5)
    truth=backtest[backtest.model=='SeasonalNaive7'].groupby('target_date').actual.sum()
    ax.plot(truth.index,truth.values,label='Факт',color='black',linewidth=2)
    ax.set(ylabel='Обращения в сутки',xlabel='Дата итогового теста')
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1));ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
    ax.legend(frameon=False,fontsize=10);fig.tight_layout()
    for ext in ['png','svg']:fig.savefig(folder/f'test_predictions.{ext}',dpi=180,bbox_inches='tight')
    plt.close(fig)

    counts=query('SELECT c.category_name,count(*) AS count,min(request_date) AS first_date,max(request_date) AS last_date FROM core.service_request r JOIN core.category c USING(category_id) GROUP BY c.category_name ORDER BY c.category_name')
    quality=query('SELECT * FROM ops.v_quality_latest ORDER BY result_id')
    snapshot={
      'measured_on':'2026-11-15','request_count':int(daily.request_count.sum()),'mart_rows':len(daily),
      'days':daily.request_date.nunique(),'series':daily[['category_name','community_board','borough']].drop_duplicates().shape[0],
      'territory_values':query('SELECT count(*) n FROM core.district')[0]['n'],
      'weather':query('SELECT count(*) n,min(observation_date) first,max(observation_date) last FROM core.weather')[0],
      'categories':counts,'quality':quality,'forecast_total':float(future.sum()),
      'forecast_by_day':[{'date':idx.date(),'count':float(v)} for idx,v in future.items()],
      'forecast_by_category':forecast.groupby('category_name').yhat.sum().to_dict(),
      'forecast_by_borough':forecast.groupby('borough').yhat.sum().to_dict(),
      'loads':query('SELECT source_name,load_mode,date_from,date_to,rows_received,rows_inserted,rows_updated,rows_rejected,status FROM ops.load_log ORDER BY started_at'),
      'selected_run':query("SELECT run_id::text,selected_model,cutoff_date,validation_start,validation_end,test_start,test_end FROM mart.model_run WHERE status='SUCCESS' ORDER BY created_at DESC LIMIT 1")[0],
      'partitions':query('SELECT count(*) n FROM ops.loaded_partition')[0]['n'],
      'lineage_assets':query('SELECT count(*) n FROM meta.lineage')[0]['n'],
      'history_versions':query('SELECT count(*) n FROM core.request_history')[0]['n']}
    dump_json(ROOT/'data/exports/report_data.json',snapshot)
    dump_json(ROOT/'data/exports/chart_data.json',{
      'monthly':{'dates':[v.strftime('%m.%Y') for v in monthly.index],
        'series':[{'name':labels[c],'values':monthly[c].fillna(0).tolist()} for c in monthly.columns]},
      'forecast':{'dates':[i.strftime('%d.%m') for i in future.index],'values':future.values.tolist()},
      'test':{'dates':[i.strftime('%d.%m') for i in truth.index],'actual':truth.values.tolist(),
        'series':[{'name':name,'values':part.groupby('target_date').yhat.sum().values.tolist()} for name,part in backtest.groupby('model')]}})
    print('Figures and report data exported',snapshot['request_count'],'requests')


if __name__=='__main__':export()
