import os
os.environ.pop("http_proxy", None); os.environ.pop("https_proxy", None); os.environ["NO_PROXY"]="*"
import akshare as ak, pandas as pd, numpy as np

idx = ak.stock_zh_index_daily(symbol="sh000688")[["date","close"]].copy()
idx["date"]=pd.to_datetime(idx["date"])
fund=ak.fund_open_fund_info_em(symbol="010623",indicator="单位净值走势")
fcols=list(fund.columns); fund=fund[[fcols[0],fcols[1]]].copy(); fund.columns=["date","nav"]
fund["date"]=pd.to_datetime(fund["date"]); fund["nav"]=pd.to_numeric(fund["nav"],errors="coerce"); fund=fund.dropna()
df=pd.merge(idx,fund,on="date",how="inner").sort_values("date").reset_index(drop=True)
close=df["close"].values; nav=df["nav"].values
N,M=12,9
s=pd.Series(close); e1=s.ewm(span=N,adjust=False).mean(); e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
tr=e3.pct_change()*100; sig=tr.rolling(M).mean()

# 口径A: 状态 (TRIX>SIG 即持有)
posA=(tr>sig).astype(int).values
# 口径B: 金叉/死叉事件触发 (只在交叉当天切换)
gold=(tr>sig)&(tr.shift(1)<=sig.shift(1)); dead=(tr<sig)&(tr.shift(1)>=sig.shift(1))
posB=np.zeros(len(close),dtype=int); cur=0
for i in range(1,len(close)):
    if gold.iloc[i]: cur=1
    elif dead.iloc[i]: cur=0
    posB[i]=cur

def run(pos):
    cash=1.0; units=0.0; entry=None; eq=[]; days_long=0
    for i in range(len(nav)):
        nd=nav[i]
        tgt=int(pos[i])
        if i>0 and tgt==1 and units==0:
            units=cash/nd; cash=0.0; entry=i
        elif i>0 and tgt==0 and units>0:
            cash=units*nd; units=0.0; entry=None
        eq.append(cash+units*nd)
        if units>0: days_long+=1
    eq=np.array(eq); ret=eq[-1]/eq[0]-1
    peak=np.maximum.accumulate(eq); mdd=((eq-peak)/peak).min()
    return ret,mdd,days_long

rA,mA,dA=run(posA); rB,mB,dB=run(posB)
print("口径A 状态(TRIX>SIG即持有): 累计%.1f%% 回撤%.1f%% 持仓天数%d/%d"%(rA*100,mA*100,dA,len(close)))
print("口径B 金叉事件触发(只在交叉日切换): 累计%.1f%% 回撤%.1f%% 持仓天数%d/%d"%(rB*100,mB*100,dB,len(close)))
# 看两者仓位不同的天数
diff=np.sum(posA!=posB)
print("两口径仓位不一致的天数:", diff)
# 打印前60天 position 对照
print("\ndate       TRIX    SIG     A  B")
for i in range(60,120):
    print("%s  %7.3f %7.3f   %d  %d"%(df['date'].iloc[i].date(),tr.iloc[i],sig.iloc[i],posA[i],posB[i]))
PY = None
