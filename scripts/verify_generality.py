"""通用性验证: N=12 是否真最优 + 结果簇在多资产上是否通用更优.
支持两类标的:
  ETF (如588000): TDX 日线+120分钟合成, 同日15:00收盘成交, 无7天费
  场外基金 (如010416/010623): akshare 单位净值(NAV), T+1按净值成交, C类7天赎回费
对每类都做: N敏感度扫描(单日线TRIX,M=9) + 结果簇对比(7组合/N12簇/单N) + (ETF)120死叉智能混合
"""
import os
os.environ.pop("http_proxy",None); os.environ.pop("http_proxy",None); os.environ["NO_PROXY"]="*"
import numpy as np, pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
NCLUSTER=[(10,9),(10,12),(12,9),(12,12),(14,9),(14,12)]   # 结果簇: N集中到12
SLIP=0.0005
REDEEM7=0.015    # C类7天赎回费

def trix_series(c,N,M):
    s=pd.Series(c,dtype=float); e1=s.ewm(span=N,adjust=False).mean()
    e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return tr.values, sig.values

def vote_avg(c, combos, thr=0.5):
    trs=[]; sigs=[]
    for n,m in combos:
        tr,sig=trix_series(c,n,m); trs.append(tr); sigs.append(sig)
    trs=np.array(trs); sigs=np.array(sigs)
    pos=(trs>sigs).astype(int)
    vote=pos.mean(0)>thr
    avg=trs.mean(0)
    return vote, avg

def sim_etf(target, price):
    cash=1.0; units=0.0; pos=0; eq=[]; sw=0; prev=0
    for i in range(len(price)):
        t=int(target[i]); nd=price[i]
        if t!=prev:
            sw+=1
            if t==1 and pos==0: fee=cash*SLIP; units=(cash-fee)/nd; cash=0.0; pos=1
            elif t==0 and pos==1: amt=units*nd; fee=amt*SLIP; cash=amt-fee; units=0.0; pos=0
        prev=t; eq.append(cash+units*nd)
    eq=np.array(eq); return eq[-1]/eq[0]-1, ((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min(), sw

def sim_fund(target, nav):
    cash=1.0; units=0.0; pos=0; eq=[]; sw=0; prev=0; buy_day=-999
    for i in range(1,len(nav)):
        t=int(target[i-1]); nd=nav[i]
        if t!=prev:
            sw+=1
            if t==1 and pos==0: fee=cash*SLIP; units=(cash-fee)/nd; cash=0.0; pos=1; buy_day=i
            elif t==0 and pos==1:
                amt=units*nd; held=(i-buy_day)
                fee=amt*SLIP + (amt*REDEEM7 if held<7 else 0.0)
                cash=amt-fee; units=0.0; pos=0
        prev=t; eq.append(cash+units*nav[i])
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

def try_fetch(symbol_str, fn, start):
    sb=symbol_str.encode()
    for market in [TDXParams.MARKET_SH, TDXParams.MARKET_SZ]:
        try:
            f=fn(sb, market, start)
            if f is not None and len(f)>50: return f
        except Exception:
            continue
    return None

def load_nav(code):
    try:
        import akshare as ak
    except ImportError:
        import subprocess, sys
        subprocess.run([sys.executable,"-m","pip","install","-q","akshare"]); import akshare as ak
    df=ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    df=df.rename(columns={"净值日期":"date","单位净值":"nav"})
    df["date"]=pd.to_datetime(df["date"]); df["nav"]=df["nav"].astype(float)
    return df.sort_values("date").reset_index(drop=True)

def analyze_etf(symbol):
    f60=try_fetch(symbol, fetch60, "2024-08-01")
    fD =try_fetch(symbol, fetch_day, "2021-01-01")
    if f60 is None or fD is None:
        print("  !! TDX无数据, 跳过"); return None
    c120,days120=to_120_day(f60)
    v120,a120=vote_avg(c120, COMBOS)
    df120=pd.DataFrame({"day":days120,"v120":v120,"a120":a120,"c120":c120})
    df120=df120.groupby("day").last().reset_index()
    cD=fD['close'].values.astype(float); datesD=fD['date'].values; vD,aD=vote_avg(cD, COMBOS)
    dfD=pd.DataFrame({"day":datesD,"vD":vD,"aD":aD,"cD":cD})
    m=pd.merge(dfD,df120,on="day",how="inner").reset_index(drop=True)
    vD=m['vD'].values.astype(int); aD=m['aD'].values
    v120=m['v120'].values.astype(int); a120=m['a120'].values
    c=m['cD'].values.astype(float)
    # 智能混合(120死叉锚定)
    daily_turning=(aD>np.r_[aD[0],aD[:-1]]); daily_falling=(aD<np.r_[aD[0],aD[:-1]])
    v120p=np.r_[v120[0],v120[:-1]]; gold120=(v120==1)&(v120p==0); dead120=(v120==0)&(v120p==1)
    aDp=np.r_[aD[0],aD[:-1]]; win=30
    tmin=np.array([aD[max(0,i-win+1):i+1].min() for i in range(len(aD))])
    tmax=np.array([aD[max(0,i-win+1):i+1].max() for i in range(len(aD))]); rng=tmax-tmin
    about_gold=(vD==0)&(aD>aDp); about_dead=(vD==1)&(aD<aDp)
    buy_smart=gold120&about_gold; sell_smart=dead120&about_dead
    pos=0; t_smart=np.zeros(len(vD),dtype=int); sw=0; prev=0
    for i in range(len(vD)):
        if pos==0 and buy_smart[i]: pos=1
        elif pos==1 and sell_smart[i]: pos=0
        t_smart[i]=pos
        if pos!=prev: sw+=1; prev=pos
    rs,mdS,swS=sim_etf(t_smart,c)
    rd,mdD,sd=sim_etf(vD.astype(int),c); hold=c[-1]/c[0]-1
    wmask=np.ones(len(cD),bool)
    w0=m['day'].iloc[0]; w1=m['day'].iloc[-1]
    wmaskD=(datesD>=w0)&(datesD<=w1); cDw=cD[wmaskD]
    def vote_from(combos,thr=0.5): return vote_avg(cD,combos,thr)[0]
    nscan=[(N,)+sim_etf((trix_series(cD,N,9)[0]>trix_series(cD,N,9)[1]).astype(int),cD) for N in [9,10,11,12,13,14,15]]
    cfg=[]
    for name,combos,thr in [("7组合基线",COMBOS,0.5),("N12结果簇",NCLUSTER,0.5),
                            ("单(12,9)",[(12,9)],0.5),("单(11,9)",[(11,9)],0.5),
                            ("单(13,9)",[(13,9)],0.5),("单(10,9)",[(10,9)],0.5)]:
        v=vote_from(combos,thr).astype(int)
        rf,mdf,swf=sim_etf(v,cD); rw,mdw,sww=sim_etf(v[wmaskD].astype(int),cDw)
        cfg.append((name,rf,mdf,swf,rw,mdw,sww))
    return dict(kind="ETF", n=len(m), start=m['day'].iloc[0].date(), end=m['day'].iloc[-1].date(),
                rs=rs,mdS=mdS,swS=swS, rd=rd,mdD=mdD,sd=sd, hold=hold, nscan=nscan, cfg=cfg)

def analyze_fund(symbol, label):
    df=load_nav(symbol)
    if df is None or len(df)<50:
        print("  !! akshare无数据, 跳过"); return None
    datesD=df['date'].values; nav=df['nav'].values.astype(float)
    w0=pd.Timestamp("2024-08-19"); w1=pd.Timestamp("2026-08-28")
    wmask=(datesD>=w0)&(datesD<=w1); navw=nav[wmask]
    def vote_from(combos,thr=0.5): return vote_avg(nav,combos,thr)[0]
    nscan=[(N,)+sim_fund((trix_series(nav,N,9)[0]>trix_series(nav,N,9)[1]).astype(int),nav) for N in [9,10,11,12,13,14,15]]
    cfg=[]
    for name,combos,thr in [("7组合基线",COMBOS,0.5),("N12结果簇",NCLUSTER,0.5),
                            ("单(12,9)",[(12,9)],0.5),("单(11,9)",[(11,9)],0.5),
                            ("单(13,9)",[(13,9)],0.5),("单(10,9)",[(10,9)],0.5)]:
        v=vote_from(combos,thr).astype(int)
        rf,mdf,swf=sim_fund(v,nav); rw,mdw,sww=sim_fund(v[wmask].astype(int),navw)
        cfg.append((name,rf,mdf,swf,rw,mdw,sww))
    hold=nav[-1]/nav[0]-1
    rd,mddD,sd=sim_fund(vote_from(COMBOS).astype(int),nav)
    return dict(kind="基金(%s)"%label, n=len(df), start=df['date'].iloc[0].date(), end=df['date'].iloc[-1].date(),
                rd=rd,mdD=mddD,sd=sd, hold=hold, nscan=nscan, cfg=cfg)

def fmt_p(x): return "%7.1f%%"%(x*100)
def fmt_d(x): return "%6.1f%%"%(x*100)

print("="*80); print("验证: N=12 是否最优 + 结果簇在多资产通用性 (ETF vs 场外基金)"); print("="*80)
JOBS=[("588000","ETF",analyze_etf),("010416","华泰柏瑞质量精选混合C",analyze_fund),("010623","基金",analyze_fund)]
for sym,label,fn in JOBS:
    print("\n"+"#"*80); print("# 标的 %s  (%s)"%(sym,label)); print("#"*80)
    if fn is analyze_fund:
        R=analyze_fund(sym,label)
    else:
        R=analyze_etf(sym)
    if R is None: continue
    print("数据区间: %s ~ %s  共 %d 条  [%s]"%(R['start'],R['end'],R['n'],R['kind']))
    print("\n[1] N敏感度扫描 (单日线TRIX, M=9)")
    print("%-5s %-11s %-11s %-8s"%("N","累计","回撤","切换"))
    for row in R['nscan']:
        N,r,md,sw=row; tag=" <== N=12" if N==12 else ""
        print("%-5d %-10s %-10s %-8d%s"%(N,fmt_p(r),fmt_d(md),sw,tag))
    print("\n[2] 结果簇对比 (全段累计 / 窗口累计, 窗口=%s~%s)"%(R.get('start'),R.get('end')))
    print("%-14s %-11s %-11s %-8s %-11s %-11s %-8s"%("配置","全段累计","全段回撤","全段切换","窗口累计","窗口回撤","窗口切换"))
    for name,rf,mdf,swf,rw,mdw,sww in R['cfg']:
        print("%-14s %-10s %-10s %-8d %-10s %-10s %-8d"%(name,fmt_p(rf),fmt_d(mdf),swf,fmt_p(rw),fmt_d(mdw),sww))
    if R['kind']=="ETF":
        print("\n[3] 智能混合(120死叉锚定) 表现")
        print("  智能混合 : 累计 %s  回撤 %s  切换 %d"%(fmt_p(R['rs']),fmt_d(R['mdS']),R['swS']))
        print("  纯日线7组: 累计 %s  回撤 %s  切换 %d"%(fmt_p(R['rd']),fmt_d(R['mdD']),R['sd']))
        print("  买入持有 : 累计 %s"%(fmt_p(R['hold'])))
    else:
        print("\n[3] 基金基准")
        print("  纯日线7组: 累计 %s  回撤 %s  切换 %d"%(fmt_p(R['rd']),fmt_d(R['mdD']),R['sd']))
        print("  买入持有 : 累计 %s"%(fmt_p(R['hold'])))
