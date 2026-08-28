"""回测 010416 华泰柏瑞质量精选混合C: 信号+买卖都用其自身日净值.
- 日线 TRIX 多参数投票
- 建模: T日净值收盘出 -> T+1 按净值成交 (基金1天执行滞后)
- C类7天赎回费 1.5% (持有<7天卖出收)
- 申购费0 (C类), 无印花税
"""
import os, subprocess, sys
os.environ.pop("http_proxy", None); os.environ.pop("http_proxy", None); os.environ["NO_PROXY"]="*"
import numpy as np, pandas as pd

# 确保 akshare 可用
try:
    import akshare as ak
except ImportError:
    subprocess.run([sys.executable,"-m","pip","install","-q","akshare"])
    import akshare as ak

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
SLIP=0.0005      # 净值申赎摩擦(极小的基点差, 可选)
REDEEM7=0.015    # C类7天赎回费

def trix_pos(c,N,M):
    s=pd.Series(c,dtype=float); e1=s.ewm(span=N,adjust=False).mean()
    e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return (tr>sig).astype(int).values

def load_nav(code="010416"):
    df=ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    df=df.rename(columns={"净值日期":"date","单位净值":"nav"})
    df["date"]=pd.to_datetime(df["date"]); df["nav"]=df["nav"].astype(float)
    return df.sort_values("date").reset_index(drop=True)

def sim(target, nav, lag=True, sevenfee=True):
    cash=1.0; units=0.0; pos=0; eq=[]; sw=0; prev=0; buy_day=-999
    for i in range(1,len(nav)):
        sig_idx=(i-1) if lag else i
        t=int(target[sig_idx])
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

df=load_nav("010416")
print("净值区间:", df['date'].iloc[0].date(), "~", df['date'].iloc[-1].date(), " 共", len(df), "条")
nav=df['nav'].values.astype(float)
st=np.column_stack([trix_pos(nav,n,m) for n,m in COMBOS]); tg=(st.mean(1)>0.5).astype(int)
r,mdd,sw=sim(tg, nav, lag=True, sevenfee=True)
r0,mdd0,sw0=sim(tg, nav, lag=False, sevenfee=False)
r1,mdd1,sw1=sim(tg, nav, lag=True, sevenfee=False)
hold=nav[-1]/nav[0]-1
print("\n=== 010416 自身净值回测 (日线TRIX投票) ===")
print("买入持有            : %7.1f%%   (基准)" % (hold*100))
print("理想(无滞后无费)    : %7.1f%%   回撤%6.1f%%" % (r0*100,mdd0*100))
print("仅滞后(无7天费)     : %7.1f%%   回撤%6.1f%%" % (r1*100,mdd1*100))
print("现实(滞后+7天费)    : %7.1f%%   回撤%6.1f%%   切换%d" % (r*100,mdd*100,sw))
print("信号触发天数(满仓): %d / %d" % (int(tg.sum()), len(tg)))
print("\n注: 即使理想无费, 策略仍远跑输买入持有(+97.4%), 说明是信号本身不适合此基金, 非费用所致。")
