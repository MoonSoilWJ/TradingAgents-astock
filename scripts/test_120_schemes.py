"""测试「120分钟抢早进场 + 日线出场」能否胜过纯日线.
方案: 进场=120金叉 & 日线即将金叉(拐头+完成度>thr); 出场=日线死叉(兜底120死叉&回落).
"""
import os
os.environ.pop("http_proxy",None); os.environ.pop("http_proxy",None); os.environ["NO_PROXY"]="*"
import numpy as np, pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

COMBOS=[(9,9),(9,12),(12,9),(12,12),(
15,9),(15,12),(20,9)]
SLIP=0.0005

def trix_series(c,N,M):
    s=pd.Series(c,dtype=float); e1=s.ewm(span=N,adjust=False).mean()
    e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return tr.values, sig.values

def vote_avg(c, combos, thr=0.5):
    trs=[]; sigs=[]
    for n,m in combos:
        tr,sig=trix_series(c,n,m); trs.append(tr); sigs.append(sig)
    trs=np.array(trs); sigs=np.array(sigs)
    return (trs>sigs).astype(int).mean(0)>thr, trs.mean(0)

def sim(target, price):
    cash=1.0; units=0.0; pos=0; eq=[]; sw=0; prev=0
    for i in range(len(price)):
        t=int(target[i]); nd=price[i]
        if t!=prev:
            sw+=1
            if t==1 and pos==0: fee=cash*SLIP; units=(cash-fee)/nd; cash=0.0; pos=1
            elif t==0 and pos==1: amt=units*nd; fee=amt*SLIP; cash=amt-fee; units=0.0; pos=0
        prev=t; eq.append(cash+units*nd)
    eq=np.array(eq); return eq[-1]/eq[0]-1, ((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min(), sw

def to_120_day(df):
    df=df.copy(); df['day']=df['date']
    out=[]; days=[]
    for _,g in df.groupby('day'):
        g=g.sort_values('datetime'); c=g['close'].values.astype(float)
        if len(c)>=2: out += [c[:2].mean(), c[-1]]; days += [g['day'].iloc[0], g['day'].iloc[0]]
        elif len(c)==1: out += [c[0]]; days += [g['day'].iloc[0]]
    return np.array(out), np.array(days)

def fetch60(symbol, market, start_date="2024-08-01", per=700, maxpages=20):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5); frames=[]
    for pg in range(maxpages):
        k=api.get_security_bars(TDXParams.KLINE_TYPE_1HOUR,market,symbol,pg*per,per)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<per: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True); f["date"]=pd.to_datetime(f["datetime"]).dt.normalize()
    f=f[f["date"]>=pd.Timestamp(start_date)]
    return f.sort_values("date").reset_index(drop=True)

def fetch_day(symbol, market, start_date="2021-01-01", per=700, maxpages=20):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5); frames=[]
    for pg in range(maxpages):
        k=api.get_security_bars(TDXParams.KLINE_TYPE_DAILY,market,symbol,pg*per,per)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<per: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True); f["date"]=pd.to_datetime(f["datetime"]).dt.normalize()
    f=f[f["date"]>=pd.Timestamp(start_date)]
    return f.sort_values("date").reset_index(drop=True)

def try_fetch(symbol, fn, start):
    sb=symbol.encode()
    for market in [TDXParams.MARKET_SH, TDXParams.MARKET_SZ]:
        try:
            f=fn(sb, market, start)
            if f is not None and len(f)>50: return f
        except Exception:
            continue
    return None

f60=try_fetch("588000", fetch60, "2024-08-01")
fD =try_fetch("588000", fetch_day, "2021-01-01")
c120,days120=to_120_day(f60); v120,a120=vote_avg(c120, COMBOS)
df120=pd.DataFrame({"day":days120,"v120":v120,"a120":a120,"c120":c120}); df120=df120.groupby("day").last().reset_index()
cD=fD['close'].values.astype(float); vD,aD=vote_avg(cD, COMBOS)
dfD=pd.DataFrame({"day":fD['date'].values,"vD":vD,"aD":aD,"cD":cD})
m=pd.merge(dfD,df120,on="day",how="inner").reset_index(drop=True)
vD=m['vD'].values.astype(int); aD=m['aD'].values; v120=m['v120'].values.astype(int)
c=m['cD'].values.astype(float)

# 基础序列
daily_turning=(aD>np.r_[aD[0],aD[:-1]]); daily_falling=(aD<np.r_[aD[0],aD[:-1]])
v120p=np.r_[v120[0],v120[:-1]]; gold120=(v120==1)&(v120p==0); dead120=(v120==0)&(v120p==1)
aDp=np.r_[aD[0],aD[:-1]]; vDp=np.r_[vD[0],vD[:-1]]; win=30
tmin=np.array([aD[max(0,i-win+1):i+1].min() for i in range(len(aD))])
tmax=np.array([aD[max(0,i-win+1):i+1].max() for i in range(len(aD))]); rng=tmax-tmin
prog_up=np.where(rng>1e-9,(aD-tmin)/rng,0.0)
prog_dn=np.where(rng>1e-9,(tmax-aD)/rng,0.0)

def backtest(target):
    pos=0; t=np.zeros(len(vD),dtype=int); sw=0; prev=0
    for i in range(len(vD)):
        if target[i]: 
            if pos==0: pos=1
        else:
            if pos==1: pos=0
        t[i]=pos
        if pos!=prev: sw+=1; prev= 0 if not target[i] else 1
    # 重新用更干净的方式
    cash=1.0; units=0.0; pos=0; eq=[]; sw=0; prev=0
    for i in range(len(c)):
        t=int(target[i]); nd=c[i]
        if t!=prev:
            sw+=1
            if t==1 and pos==0: fee=cash*SLIP; units=(cash-fee)/nd; cash=0.0; pos=1
            elif t==0 and pos==1: amt=units*nd; fee=amt*SLIP; cash=amt-fee; units=0.0; pos=0
        prev=t; eq.append(cash+units*nd)
    eq=np.array(eq); return eq[-1]/eq[0]-1, ((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min(), sw

# 方案1: 纯日线
r1,md1,sw1=backtest(vD)
# 方案2: 智能(120金叉买日将叉 / 120死叉卖日将死) -> 已知116%
about_gold=(vD==0)&(aD>aDp); about_dead=(vD==1)&(aD<aDp)
smart=gold120&about_gold
sell_smart=dead120&about_dead
t_smart=np.zeros(len(vD),dtype=int); pos=0
for i in range(len(vD)):
    if pos==0 and smart[i]: pos=1
    elif pos==1 and sell_smart[i]: pos=0
    t_smart[i]=pos
r2,md2,sw2=backtest(t_smart)
# 方案3: 120抢早进场 + 日线出场(新方案)
buy_new=gold120&about_gold&(prog_up>0.5)
daily_death=(vD==0)&(vDp==1)
sell_new=daily_death | (dead120&daily_falling)
t_new=np.zeros(len(vD),dtype=int); pos=0
for i in range(len(vD)):
    if pos==0 and buy_new[i]: pos=1
    elif pos==1 and sell_new[i]: pos=0
    t_new[i]=pos
r3,md3,sw3=backtest(t_new)

print("== 588000 120分钟方向方案对比 (2024-08-19~窗口) ==")
print("%-30s %-11s %-11s %-8s"%("方案","累计","回撤","切换"))
print("%-30s %-10.1f%% %-10.1f%% %-8d"%("纯日线(N12簇等价 vD)",r1*100,md1*100,sw1))
print("%-30s %-10.1f%% %-10.1f%% %-8d"%("旧智能(120死叉出场)",r2*100,md2*100,sw2))
print("%-30s %-10.1f%% %-10.1f%% %-8d"%("新:120抢早+日线出场",r3*100,md3*100,sw3))
