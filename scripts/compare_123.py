"""选项1/2/3 受控对比: 同一信号(科创50), 同时间段, 同决策日.
1: 588000信号 -> 588000 (同日收盘, 无7天费)
2: 科创50指数信号 -> 588000 (同日收盘, 无7天费)
3: 科创50指数信号 -> 恒越成长精选C(010623)
    3a 受控: 同日净值成交(无滞后无费, 纯看资产跟踪)
    3b 真实: 次日净值成交 + 7天赎回费
"""
import os
os.environ.pop("http_proxy",None); os.environ.pop("http_proxy",None); os.environ["NO_PROXY"]="*"
import numpy as np, pandas as pd, akshare as ak
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
SLIP=0.0005; REDEEM7=0.015

def trix_pos(c,N,M):
    s=pd.Series(c,dtype=float); e1=s.ewm(span=N,adjust=False).mean()
    e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return (tr>sig).astype(int).values

def etf_daily(code="588000"):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5); frames=[]
    for pg in range(20):
        k=api.get_security_bars(TDXParams.KLINE_TYPE_DAILY,TDXParams.MARKET_SH,b"588000",pg*700,700)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<700: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True); f["date"]=pd.to_datetime(f["datetime"]).dt.normalize()
    return f[["date","close"]].sort_values("date").drop_duplicates("date").reset_index(drop=True)

def index_daily(symbol="000688"):
    try:
        df=ak.index_zh_a_hist(symbol=symbol, period="daily", start_date="20210101", end_date="20260826")
        df=df.rename(columns={"日期":"date","收盘":"close"})
        df["date"]=pd.to_datetime(df["date"]); df["close"]=df["close"].astype(float)
        return df[["date","close"]].sort_values("date").reset_index(drop=True)
    except Exception as e:
        print("[warn] index akshare失败, 用588000作指数代理:", e); return None

def nav_daily(code="010623"):
    df=None
    for _ in range(3):
        try:
            df=ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            if df is not None and len(df)>0: break
        except Exception as e: print("[warn] nav retry:", e)
    df=df.rename(columns={"净值日期":"date","单位净值":"nav"})
    df["date"]=pd.to_datetime(df["date"]); df["nav"]=df["nav"].astype(float)
    return df[["date","nav"]].sort_values("date").reset_index(drop=True)

def sim_sameday(target, price, slip=SLIP):
    cash=1.0; units=0.0; pos=0; eq=[]; sw=0; prev=0; buy_day=-999
    for i in range(len(price)):
        t=int(target[i]); nd=price[i]
        if t!=prev:
            sw+=1
            if t==1 and pos==0: fee=cash*slip; units=(cash-fee)/nd; cash=0.0; pos=1; buy_day=i
            elif t==0 and pos==1:
                amt=units*nd; fee=amt*slip+(amt*REDEEM7 if (i-buy_day)<7 else 0.0)
                cash=amt-fee; units=0.0; pos=0
        prev=t; eq.append(cash+units*nd)
    eq=np.array(eq); return eq[-1]/eq[0]-1, ((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min(), sw

def sim_lagfee(target, nav):
    cash=1.0; units=0.0; pos=0; eq=[]; sw=0; prev=0; buy_day=-999
    for i in range(1,len(nav)):
        t=int(target[i-1]); nd=nav[i]
        if t!=prev:
            sw+=1
            if t==1 and pos==0: fee=cash*SLIP; units=(cash-fee)/nd; cash=0.0; pos=1; buy_day=i
            elif t==0 and pos==1:
                amt=units*nd; fee=amt*SLIP+(amt*REDEEM7 if (i-buy_day)<7 else 0.0)
                cash=amt-fee; units=0.0; pos=0
        prev=t; eq.append(cash+units*nav[i])
    eq=np.array(eq); return eq[-1]/eq[0]-1, ((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min(), sw

etf=etf_daily(); idx=index_daily(); nav=nav_daily("010623")
print("etf",len(etf),"idx",(0 if idx is None else len(idx)),"nav",len(nav))
# 对齐到 nav 区间
lo=nav['date'].iloc[0]; hi=nav['date'].iloc[-1]
e=etf[(etf['date']>=lo)&(etf['date']<=hi)].reset_index(drop=True)
if idx is None: idx=e.copy()
else: idx=idx[(idx['date']>=lo)&(idx['date']<=hi)].reset_index(drop=True)
n=nav[(nav['date']>=lo)&(nav['date']<=hi)].reset_index(drop=True)
data=pd.merge(e, idx, on="date", how="inner", suffixes=("_etf","_idx"))
data=pd.merge(data, n, on="date", how="inner").reset_index(drop=True)
print("对齐区间:", data['date'].iloc[0].date(),"~",data['date'].iloc[-1].date()," 共",len(data),"日")

tg1=(np.column_stack([trix_pos(data['close_etf'].values,n,m) for n,m in COMBOS]).mean(1)>0.5).astype(int)
tg2=(np.column_stack([trix_pos(data['close_idx'].values,n,m) for n,m in COMBOS]).mean(1)>0.5).astype(int)

r1,md1,s1=sim_sameday(tg1, data['close_etf'].values)              # 选项1
r2,md2,s2=sim_sameday(tg2, data['close_etf'].values)              # 选项2
r3a,md3a,s3a=sim_sameday(tg2, data['nav'].values)                 # 选项3a 受控同日
r3b,md3b,s3b=sim_lagfee(tg2, data['nav'].values)                  # 选项3b 真实
hold_etf=data['close_etf'].values[-1]/data['close_etf'].values[0]-1
hold_nav=data['nav'].values[-1]/data['nav'].values[0]-1

print("\n=== 受控对比: 同信号(科创50) / 同时间段 / 同决策日 ===")
print("选项1 588000信号->588000 : 累计%7.1f%%  回撤%6.1f%%  切换%d" % (r1*100,md1*100,s1))
print("选项2 指数信号  ->588000 : 累计%7.1f%%  回撤%6.1f%%  切换%d" % (r2*100,md2*100,s2))
print("选项3a 指数信号->恒越(同日): 累计%7.1f%%  回撤%6.1f%%  切换%d" % (r3a*100,md3a*100,s3a))
print("选项3b 指数信号->恒越(真实): 累计%7.1f%%  回撤%6.1f%%  切换%d" % (r3b*100,md3b*100,s3b))
print("买入持有 588000           : 累计%7.1f%%" % (hold_etf*100))
print("买入持有 恒越(010623)      : 累计%7.1f%%" % (hold_nav*100))
print("\n注: 1与2仅信号源(ETF收盘 vs 指数收盘)之差, 结果应几乎相同, 验证'等价'。")
