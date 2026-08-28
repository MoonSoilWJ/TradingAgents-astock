"""对比: 同日收盘成交(当前) vs 次日成交(无前视, 保守) 对 588000 ETF。
TDX取真实OHLC, 次日用开盘价成交。
"""
import os
os.environ.pop("http_proxy", None); os.environ.pop("http_proxy", None); os.environ["NO_PROXY"]="*"
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams
import pandas as pd, numpy as np

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
SLIP=0.0005

api=TdxHq_API()
api.connect("180.153.18.170",7709,time_out=5)
frames=[]
for start in range(0,8*800,800):
    k=api.get_security_bars(TDXParams.KLINE_TYPE_DAILY,TDXParams.MARKET_SH,b"588000",start,800)
    if k is None: break
    d=api.to_df(k)
    if d is None or len(d)==0: break
    frames.append(d)
    if len(d)<800: break
api.disconnect()
f=pd.concat(frames,ignore_index=True)
f["date"]=pd.to_datetime(f["datetime"])
f=f.sort_values("date").drop_duplicates("date").reset_index(drop=True)
# 只用2021-02起
f=f[f["date"]>=pd.Timestamp("20210201")].reset_index(drop=True)
close=f["close"].values.astype(float); open_=f["open"].values.astype(float); dates=f["date"].values

def trix_pos(c,N,M):
    s=pd.Series(c); e1=s.ewm(span=N,adjust=False).mean(); e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return (tr>sig).astype(int).values

states=np.column_stack([trix_pos(close,n,m) for n,m in COMBOS]); target=(states.mean(1)>0.5).astype(int)

def sim(target,price,exec_mode):
    # exec_mode: 'same' 同日收盘; 'next' 次日开盘(无前视)
    cash=1.0;units=0.0;pos=0;entry=None;eq=[]
    for i in range(len(price)):
        t=int(target[i]); nd=price[i]
        px = close[i] if exec_mode=="same" else open_[i]
        if i>0 and t==1 and pos==0:
            fee=cash*SLIP; units=(cash-fee)/px; cash=0.0; pos=1; entry=i
        elif i>0 and t==0 and pos==1:
            amt=units*px; fee=amt*SLIP; cash=amt-fee; units=0.0; pos=0; entry=None
        eq.append(cash+units*close[i])
    return np.array(eq)

def met(eq):
    eq=np.array(eq); r=eq[-1]/eq[0]-1; yrs=len(eq)/252; a=(eq[-1]/eq[0])**(1/yrs)-1
    peak=np.maximum.accumulate(eq); mdd=((eq-peak)/peak).min(); return r,a,mdd

for mode in ["same","next"]:
    eq=sim(target,close,mode); r,a,m=met(eq)
    print(f"{'同日收盘成交' if mode=='same' else '次日开盘成交(无前视)':<22} 累计{r*100:>7.1f}%  年化{a*100:>6.1f}%  回撤{m*100:>7.1f}%")
print(f"持有ETF(收盘): {met(close/close[0])[0]*100:.1f}%")
