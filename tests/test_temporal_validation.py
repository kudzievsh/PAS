import numpy as np
import pandas as pd
from src.modeling import make_features,horizon_data,fit_predict,metrics,BASELINE,RIDGE


def example():
    dates=pd.date_range('2023-01-01',periods=150)
    frames=[]
    for district in [1,2]:
        frames.append(pd.DataFrame({'request_date':dates,'category_id':1,'district_id':district,
          'request_count':np.arange(150)%7+district+1,'temperature_min':10.,'temperature_max':15.,'precipitation':0.,'snow':0.}))
    return pd.concat(frames,ignore_index=True)


def test_predictions_do_not_use_future_targets_or_weather():
    before=example();origin=pd.Timestamp('2023-04-20')
    after=before.copy()
    future=after.request_date>origin
    after.loc[future,['request_count','temperature_min','temperature_max','precipitation']]=999999.
    a={h:horizon_data(make_features(before),h) for h in range(1,8)}
    b={h:horizon_data(make_features(after),h) for h in range(1,8)}
    pa=fit_predict(a,origin);pb=fit_predict(b,origin)
    np.testing.assert_allclose(pa.yhat,pb.yhat,rtol=0,atol=1e-10)
    assert set(pa.model)=={BASELINE,RIDGE}
    assert (pa.yhat>=0).all()
    assert set(pa.target_date)==set(pd.date_range(origin+pd.Timedelta(days=1),periods=7))


def test_zero_actuals_do_not_create_infinite_wape():
    result=metrics([0,0],[1,0])
    assert result['wape'] is None
    assert result['mae']==0.5
    assert result['rmse']==np.sqrt(0.5)
