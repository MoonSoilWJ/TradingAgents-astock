"""同时间段(2021-01-01~2026-08-27)对比: 120分钟 vs 日线 TRIX投票.
60分钟用TDX分页往回拉; 日线也用TDX同期, 保证同源可比.
"""
import os
os.environ.pop("http_proxy", None); os.environ.pop("http_proxy", None); os.environ["NO_PROXY"]="*"
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams
import pandas as pd, numpy as np

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
SLIP=0.0005

def fetch60_back(start_date="2021-01-01", per=700, maxpages=20):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5)
    frames=[]
    for pg in range(maxpages):
        k=api.get_security_bars(TDXParams.KLINE_TYPE_1HOUR,TDXParams.MARKET_SH,b"588000",pg*per,per)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<per: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True)
    f["date"]=pd.to_datetime(f["datetime"])
    f=f.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    f=f[f["date"]>=pd.Timestamp(start_date)]
    return f

def fetch_day(start_date="2021-01-01"):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5)
    frames=[]
    for pg in range(20):
        k=api.get_security_bars(TDXParams.KLINE_TYPE_DAILY,TDXParams.MARKET_SH,b"588000",pg*700,700)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<700: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True)
    f["date"]=pd.to_datetime(f["datetime"])
    f=f.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    f=f[f["date"]>=pd.Timestamp(start_date)]
    return f

def trix_pos(c,N,M):
    s=pd.Series(c); e1=s.ewm(span=N,adjust=False).mean(); e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return (tr>sig).astype(int).values

def to_120_naive(arr):
    a=np.asarray(arr,dtype=float)
    if len(a)%2!=0: a=a[1:]
    return a.reshape(-1,2).mean(axis=1)

def to_120_daily(df):
    df=df.copy(); df['day']=df['date'].dt.date; df['h']=df['date'].dt.hour
    out=[]
    for _,g in df.groupby('day'):
        g=g.sort_values('date'); c=g['close'].values.astype(float)
        if len(c)>=2: out.append(c[:2].mean()); out.append(c[-1])
        elif len(c)==1: out.append(c[0])
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

print("fetching 60m (2021~) ...")
f60=fetch60_back()
print("60m根数",len(f60),"范围",f60['date'].iloc[0],"~",f60['date'].iloc[-1])
print("fetching daily (2021~) ...")
fD=fetch_day()
print("日线根数",len(fD),"范围",fD['date'].iloc[0],"~",fD['date'].iloc[-1])

# 120m 两种
cA=to_120_naive(f60['close'].values.astype(float))
cB=to_120_daily(f60)
# 日线
cD=fD['close'].values.astype(float)

stA=np.column_stack([trix_pos(cA,n,m) for n,m in COMBOS]); tgA=(stA.mean(1)>0.5).astype(int)
stB=np.column_stack([trix_pos(cB,n,m) for n,m in COMBOS]); tgB=(stB.mean(1)>0.5).astype(int)
stD=np.column_stack([trix_pos(cD,n,m) for n,m in COMBOS]); tgD=(stD.mean(1)>0.5).astype(int)

rA,mA,swA=sim(tgA,cA)
rB,mB,swB=sim(tgB,cB)
rD,mD,swD=sim(tgD,cD)

# 公平对比: 日线也截到 120m 的同一窗口起点
w0=f60['date'].iloc[0].normalize()
Dw=fD[fD['date']>=w0].reset_index(drop=True)
cDw=Dw['close'].values.astype(float)
stDw=np.column_stack([trix_pos(cDw,n,m) for n,m in COMBOS]); tgDw=(stDw.mean(1)>0.5).astype(int)
rDw,mDw,swDw=sim(tgDw,cDw)

# 存盘
import os as _os
_os.makedirs("results",exist_ok=True)
pd.DataFrame({"close120_naive":cA,"close120_day":cB}).to_csv("results/p120_2024.csv",index=False)
pd.DataFrame({"date":Dw['date'].values,"close_daily":cDw}).to_csv("results/pdaily_2024.csv",index=False)

print("\n=== 同时间段对比 (TRIX多参数投票) ===")
print("窗口: %s ~ %s" % (w0.date(), f60['date'].iloc[-1].date()))
print("120m 无脑合成 : 累计%7.1f%%  回撤%6.1f%%  切换%3d  同期持有%7.1f%%" % (rA*100,mA*100,swA,(cA[-1]/cA[0]-1)*100))
print("120m 按日对齐 : 累计%7.1f%%  回撤%6.1f%%  切换%3d  同期持有%7.1f%%" % (rB*100,mB*100,swB,(cB[-1]/cB[0]-1)*100))
print("日线(同窗口)  : 累计%7.1f%%  回撤%6.1f%%  切换%3d  同期持有%7.1f%%" % (rDw*100,mDw*100,swDw,(cDw[-1]/cDw[0]-1)*100))
print("日线(全段)    : 累计%7.1f%%  回撤%6.1f%%  切换%3d  同期持有%7.1f%%" % (rD*100,mD*100,swD,(cD[-1]/cD[0]-1)*100))
print("\n注: TDX免费源60分钟仅到2024-08-19, 故120m无法覆盖2021全段;")
print("    聚宽-17.97%应属取数bug(见下方说明)。")
