"""交叉信号: 用 588000(科创50ETF) 日线TRIX投票做信号, 交易 010416(华泰柏瑞质量精选C) 的净值.
- 信号: 588000 收盘 -> TRIX 7组合投票 -> target
- 执行: 010416 净值, T+1 成交(1天滞后), C类7天赎回费
- 对照: 010416 自身信号(+39.4%) / 010416 买入持有(+97.4%)
"""
import os, sys
os.environ.pop("http_proxy", None); os.environ.pop("http_proxy", None); os.environ["NO_PROXY"]="*"
import numpy as np, pandas as pd
import akshare as ak

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
SLIP=0.0005; REDEEM7=0.015

def trix_pos(c,N,M):
    s=pd.Series(c,dtype=float); e1=s.ewm(span=N,adjust=False).mean()
    e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return (tr>sig).astype(int).values

def load_etf(code="588000"):
    try:
        df=ak.fund_etf_hist_em(symbol=code, period="daily", adjust="")
        df=df.rename(columns={"日期":"date","收盘":"close"})
        df["date"]=pd.to_datetime(df["date"]); df["close"]=df["close"].astype(float)
        return df[["date","close"]].sort_values("date").reset_index(drop=True)
    except Exception as e:
        print("[warn] akshare ETF失败, 回退TDX:", e)
        from pytdx.hq import TdxHq_API
        from pytdx.params import TDXParams
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
        f=pd.concat(frames,ignore_index=True); f["date"]=pd.to_datetime(f["datetime"]).dt.normalize()
        f=f.sort_values("date").drop_duplicates("date")
        return f[["date","close"]].reset_index(drop=True)

def load_nav(code="010416"):
    df=None
    for _ in range(3):
        try:
            df=ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            if df is not None and len(df)>0: break
        except Exception as e:
            print("[warn] nav retry:", e)
    df=df.rename(columns={"净值日期":"date","单位净值":"nav"})
    df["date"]=pd.to_datetime(df["date"]); df["nav"]=df["nav"].astype(float)
    return df[["date","nav"]].sort_values("date").reset_index(drop=True)

def sim(target, nav, lag=True, sevenfee=True):
    cash=1.0; units=0.0; pos=0; eq=[]; sw=0; prev=0; buy_day=-999
    for i in range(1,len(nav)):
        t=int(target[(i-1) if lag else i])
        nd=nav[i]
        if t!=prev:
            sw+=1
            if t==1 and pos==0:
                fee=cash*SLIP; units=(cash-fee)/nd; cash=0.0; pos=1; buy_day=i
            elif t==0 and pos==1:
                held=(i-buy_day); amt=units*nd
                fee=amt*SLIP + (amt*REDEEM7 if (sevenfee and held<7) else 0.0)
                cash=amt-fee; units=0.0; pos=0
        prev=t
        eq.append(cash+units*nav[i])
    eq=np.array(eq); r=eq[-1]/eq[0]-1
    mdd=((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min()
    return r,mdd,sw

etf=load_etf("588000"); nav=load_nav("010416")
print("etf条数",len(etf)," nav条数",len(nav))
data=pd.merge(etf,nav,on="date",how="inner").reset_index(drop=True)
print("对齐区间:", data['date'].iloc[0].date(),"~",data['date'].iloc[-1].date()," 共",len(data),"交易日")
sig=np.column_stack([trix_pos(data['close'].values,n,m) for n,m in COMBOS]); tg=(sig.mean(1)>0.5).astype(int)
r,mdd,sw=sim(tg, data['nav'].values)
hold=data['nav'].values[-1]/data['nav'].values[0]-1
# 对照: 010416 自身信号
st=np.column_stack([trix_pos(data['nav'].values,n,m) for n,m in COMBOS]); tg2=(st.mean(1)>0.5).astype(int)
r2,mdd2,sw2=sim(tg2, data['nav'].values)
print("\n=== 科创50信号 -> 交易010416 (滞后+7天费) ===")
print("交叉信号(科创50做信号): 累计%7.1f%%  回撤%6.1f%%  切换%d" % (r*100,mdd*100,sw))
print("自身信号(010416做信号): 累计%7.1f%%  回撤%6.1f%%  切换%d" % (r2*100,mdd2*100,sw2))
print("买入持有(010416)      : 累计%7.1f%%" % (hold*100))
print("超额(交叉 vs 持有)    : %7.1f%%" % ((r-hold)*100))
print("满仓天数(交叉)        : %d / %d (%.0f%%)" % (int(tg.sum()),len(tg),100*tg.mean()))
