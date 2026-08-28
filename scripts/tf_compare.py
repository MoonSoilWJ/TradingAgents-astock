"""对比 日线 vs 60分钟 TRIX 投票策略, 对 588000 ETF。
注意: TDX免费服务器只给近~800根60分钟(约近1年), 故60分钟样本较短, 仅作频率/磨损示意。
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
        frames.append(d); 
        if len(d)<800: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True)
    f["date"]=pd.to_datetime(f["datetime"])
    f=f.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return f

def trix_pos(c,N,M):
    s=pd.Series(c); e1=s.ewm(span=N,adjust=False).mean(); e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return (tr>sig).astype(int).values

def sim(target,price):
    cash=1.0;units=0.0;pos=0;eq=[];switches=0;prev=0
    for i in range(len(price)):
        t=int(target[i]); nd=price[i]
        if i>0 and t!=prev:
            switches+=1
            if t==1 and pos==0:
                fee=cash*SLIP; units=(cash-fee)/nd; cash=0.0; pos=1
            elif t==0 and pos==1:
                amt=units*nd; fee=amt*SLIP; cash=amt-fee; units=0.0; pos=0
        prev=t
        eq.append(cash+units*nd)
    return np.array(eq),switches

def met(eq):
    eq=np.array(eq); r=eq[-1]/eq[0]-1; yrs=len(eq)/ (252 if False else 1); return r,((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min()

print("=== 日线 (全样本 2021-02 起) ===")
fd=fetch(TDXParams.KLINE_TYPE_DAILY, 800*8)
fd=fd[fd["date"]>=pd.Timestamp("20210201")]
cd=fd["close"].values.astype(float)
st=np.column_stack([trix_pos(cd,n,m) for n,m in COMBOS]); tg=(st.mean(1)>0.5).astype(int)
eq,sw=sim(tg,cd); r,m=met(eq)
print(f"  累计{r*100:>7.1f}%  回撤{m*100:>7.1f}%  切换次数{sw}  区间{len(cd)}日")

print("=== 60分钟 (近~800根, 约近1年) ===")
fh=fetch(TDXParams.KLINE_TYPE_1HOUR, 800)
ch=fh["close"].values.astype(float)
st=np.column_stack([trix_pos(ch,n,m) for n,m in COMBOS]); tg=(st.mean(1)>0.5).astype(int)
eq,sw=sim(tg,ch); r,m=met(eq)
print(f"  区间 {fh['date'].iloc[0]} ~ {fh['date'].iloc[-1]}  共{len(ch)}根")
print(f"  累计{r*100:>7.1f}%  回撤{m*100:>7.1f}%  切换次数{sw}  (年化折算需乘样本长度系数, 此处仅看相对)")
