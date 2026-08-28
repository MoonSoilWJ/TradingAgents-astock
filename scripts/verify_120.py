"""本地用TDX真实60分钟, 严格按聚宽jq_120min逻辑验证120分钟策略是否应为负。
注意: TDX免费源只给近~800根60分钟(约200交易日), 故此处为近段样本, 仅验证'是否应接近0/负'。
同时打印: 实际60分钟根数, 合成120分钟根数, long_ratio序列概览。
"""
import os
os.environ.pop("http_proxy", None); os.environ.pop("http_proxy", None); os.environ["NO_PROXY"]="*"
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams
import pandas as pd, numpy as np

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
SLIP=0.0005

def fetch60(count=800):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5)
    frames=[]
    for s in range(0,count,800):
        k=api.get_security_bars(TDXParams.KLINE_TYPE_1HOUR,TDXParams.MARKET_SH,b"588000",s,800)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<800: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True); f["date"]=pd.to_datetime(f["datetime"])
    return f.sort_values("date").drop_duplicates("date").reset_index(drop=True)

def trix_pos(c,N,M):
    s=pd.Series(c); e1=s.ewm(span=N,adjust=False).mean(); e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return (tr>sig).astype(int).values

def to_120(arr):
    a=np.asarray(arr,dtype=float)
    if len(a)%2!=0: a=a[1:]
    return a.reshape(-1,2).mean(axis=1)

def sim(target,price):
    cash=1.0;units=0.0;pos=0;eq=[];sw=0;prev=0
    for i in range(len(price)):
        t=int(target[i]); nd=price[i]
        if i>0 and t!=prev:
            sw+=1
            if t==1 and pos==0: fee=cash*SLIP; units=(cash-fee)/nd; cash=0.0; pos=1
            elif t==0 and pos==1: amt=units*nd; fee=amt*SLIP; cash=amt-fee; units=0.0; pos=0
        prev=t
        eq.append(cash+units*nd)
    eq=np.array(eq)
    return eq,sw

f=fetch60(800)
print("60分钟原始根数:", len(f), "时间范围", f['date'].iloc[0], "~", f['date'].iloc[-1])
# 看每天60分钟根数分布
f['day']=f['date'].dt.date
perday=f.groupby('day').size()
print("每日60分钟根数分布(应多为4): 众数=", perday.mode().tolist(), "有非4的天数=", (perday!=4).sum())
# 模拟聚宽: 用全部60分钟, 合成120分钟
c60=f['close'].values.astype(float)
print("含NaN?", np.isnan(c60).any(), "末尾5根:", c60[-5:])
c120=to_120(c60)
print("合成120分钟根数:", len(c120))
states=np.column_stack([trix_pos(c120,n,m) for n,m in COMBOS]); lr=states.mean(1)
print("long_ratio: 均值=%.3f  >0.5占比=%.1f%%" % (lr.mean(), (lr>0.5).mean()*100))
target=(lr>0.5).astype(int)
eq,sw=sim(target,c120)
r=eq[-1]/eq[0]-1; peak=np.maximum.accumulate(eq); mdd=((eq-peak)/peak).min()
print(">>> 120分钟策略(近段样本): 累计%.1f%%  回撤%.1f%%  切换%d次" % (r*100,mdd*100,sw))
print(">>> 若全程持有(同区间): %.1f%%" % ((c120[-1]/c120[0]-1)*100))
