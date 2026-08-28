"""对比两种120分钟合成方式, 用TDX真实60分钟(近800根, 约200交易日):
A) 聚宽版: 无脑每2根reshape(mean)   -> 跨日边界会错位
B) 按自然日对齐: 每天4根60m -> 合成2根120m(取该2小时的收盘均值/末值)
并各自跑TRIX投票, 看收益差异。
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

def to_120_naive(arr):
    a=np.asarray(arr,dtype=float)
    if len(a)%2!=0: a=a[1:]
    return a.reshape(-1,2).mean(axis=1)

def to_120_daily(df):
    # 每天4根60m(10:30,11:30,14:00,15:00) -> 2根120m: (10:30,11:30)->午, (14:00,15:00)->下午
    df=df.copy(); df['day']=df['date'].dt.date; df['h']=df['date'].dt.hour
    out=[]
    for _,g in df.groupby('day'):
        g=g.sort_values('date')
        c=g['close'].values.astype(float)
        if len(c)>=2:
            out.append(c[:2].mean())   # 上午120m收盘(用前两小时均值近似)
            out.append(c[-1])          # 下午120m收盘=当日15:00收盘
        elif len(c)==1:
            out.append(c[0])
    return np.array(out)

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

f=fetch60(800)
print("60m根数",len(f),"范围",f['date'].iloc[0],"~",f['date'].iloc[-1])

# A 无脑
cA=to_120_naive(f['close'].values.astype(float))
stA=np.column_stack([trix_pos(cA,n,m) for n,m in COMBOS]); tgA=(stA.mean(1)>0.5).astype(int)
rA,mA,swA=sim(tgA,cA)
# B 按日
cB=to_120_daily(f)
stB=np.column_stack([trix_pos(cB,n,m) for n,m in COMBOS]); tgB=(stB.mean(1)>0.5).astype(int)
rB,mB,swB=sim(tgB,cB)
print("A 无脑reshape: 累计%.1f%% 回撤%.1f%% 切换%d" % (rA*100,mA*100,swA))
print("B 按日对齐:    累计%.1f%% 回撤%.1f%% 切换%d" % (rB*100,mB*100,swB))
print("持有(同区间60m): %.1f%%" % ((f['close'].values[-1]/f['close'].values[0]-1)*100))
