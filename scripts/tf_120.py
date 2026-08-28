"""日线 vs 60分钟 vs 120分钟 TRIX投票, 588000 ETF。
120分钟由60分钟下采样合成(每2根合并)。样本受TDX免费源限制: 分钟K只近~800根60分钟(约200交易日)。
"""
import os
os.environ.pop("http_proxy", None); os.environ.pop("http_proxy", None); os.environ["NO_PROXY"]="*"
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams
import pandas as pd, numpy as np

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
SLIP=0.0005

def fetch(ktype, count=800):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5)
    frames=[]
    for s in range(0,count,800):
        k=api.get_security_bars(ktype,TDXParams.MARKET_SH,b"588000",s,800)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<800: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True)
    f["date"]=pd.to_datetime(f["datetime"])
    return f.sort_values("date").drop_duplicates("date").reset_index(drop=True)

def to_120(min60):
    g=np.arange(len(min60))//2
    agg=min60.groupby(g).agg(open=("open","first"),close=("close","last"),
                             high=("high","max"),low=("low","min"),
                             datetime=("datetime","last")).reset_index(drop=True)
    return agg

def trix_pos(c,N,M):
    s=pd.Series(c); e1=s.ewm(span=N,adjust=False).mean(); e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return (tr>sig).astype(int).values

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
    eq=np.array(eq); r=eq[-1]/eq[0]-1; mdd=((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min()
    return r,mdd,sw

print("=== 日线 (2021-02 全样本) ===")
fd=fetch(TDXParams.KLINE_TYPE_DAILY,800*8); fd=fd[fd["date"]>=pd.Timestamp("20210201")]
cd=fd["close"].values.astype(float)
st=np.column_stack([trix_pos(cd,n,m) for n,m in COMBOS]); tg=(st.mean(1)>0.5).astype(int)
r,m,sw=sim(tg,cd)
print(f"  累计{r*100:>7.1f}%  回撤{m*100:>7.1f}%  切换{sw}次  {len(cd)}日")

print("=== 60分钟 (近~800根60分=约200交易日) ===")
fh=fetch(TDXParams.KLINE_TYPE_1HOUR,800); ch=fh["close"].values.astype(float)
st=np.column_stack([trix_pos(ch,n,m) for n,m in COMBOS]); tg=(st.mean(1)>0.5).astype(int)
r,m,sw=sim(tg,ch)
print(f"  {fh['datetime'].iloc[0]}~{fh['datetime'].iloc[-1]}  累计{r*100:>7.1f}%  回撤{m*100:>7.1f}%  切换{sw}次  {len(ch)}根")

print("=== 120分钟 (由60分钟合成, 同区间) ===")
f120=to_120(fh); c120=f120["close"].values.astype(float)
st=np.column_stack([trix_pos(c120,n,m) for n,m in COMBOS]); tg=(st.mean(1)>0.5).astype(int)
r,m,sw=sim(tg,c120)
print(f"  累计{r*100:>7.1f}%  回撤{m*100:>7.1f}%  切换{sw}次  {len(c120)}根")
