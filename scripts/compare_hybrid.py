"""多周期混合: 日线TRIX gate + 120分钟TRIX 提前触发. 同窗口同执行对比.
日线: TDX日线; 120分钟: TDX 60分钟合成(按日对齐, 每天2根).
所有策略用 7组合投票, 同日在15:00收盘成交(ETF, 无7天费).
"""
import os
os.environ.pop("http_proxy",None); os.environ.pop("http_proxy",None); os.environ["NO_PROXY"]="*"
import numpy as np, pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

COMBOS=[(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
SLIP=0.0005

def trix_series(c,N,M):
    s=pd.Series(c,dtype=float); e1=s.ewm(span=N,adjust=False).mean()
    e2=e1.ewm(span=N,adjust=False).mean(); e3=e2.ewm(span=N,adjust=False).mean()
    tr=e3.pct_change()*100; sig=tr.rolling(M).mean(); return tr.values, sig.values

def vote_and_avg(c, thr=0.5):
    trs=[]; sigs=[]
    for n,m in COMBOS:
        tr,sig=trix_series(c,n,m); trs.append(tr); sigs.append(sig)
    trs=np.array(trs); sigs=np.array(sigs)
    pos=(trs>sigs).astype(int)
    vote=pos.mean(0)>thr
    avg_trix=trs.mean(0)        # 平均TRIX值, 用于"即将金叉"判定
    return vote, avg_trix

def fetch60(start_date="2024-08-01", per=700, maxpages=20):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5); frames=[]
    for pg in range(maxpages):
        k=api.get_security_bars(TDXParams.KLINE_TYPE_1HOUR,TDXParams.MARKET_SH,b"588000",pg*per,per)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<per: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True); f["date"]=pd.to_datetime(f["datetime"]).dt.normalize()
    f=f[f["date"]>=pd.Timestamp(start_date)]
    return f.sort_values("date").reset_index(drop=True)

def fetch_day(start_date="2021-01-01"):
    api=TdxHq_API(); api.connect("180.153.18.170",7709,time_out=5); frames=[]
    for pg in range(20):
        k=api.get_security_bars(TDXParams.KLINE_TYPE_DAILY,TDXParams.MARKET_SH,b"588000",pg*per if False else pg*700,700)
        if k is None: break
        d=api.to_df(k)
        if d is None or len(d)==0: break
        frames.append(d)
        if len(d)<700: break
    api.disconnect()
    f=pd.concat(frames,ignore_index=True); f["date"]=pd.to_datetime(f["datetime"]).dt.normalize()
    f=f[f["date"]>=pd.Timestamp(start_date)]
    return f.sort_values("date").reset_index(drop=True)

def to_120_day(df):
    df=df.copy(); df['day']=df['date']; df['h']=pd.to_datetime(df['datetime']).dt.hour
    out=[]; days=[]
    for _,g in df.groupby('day'):
        g=g.sort_values('datetime'); c=g['close'].values.astype(float)
        if len(c)>=2: out += [c[:2].mean(), c[-1]]; days += [g['day'].iloc[0], g['day'].iloc[0]]
        elif len(c)==1: out += [c[0]]; days += [g['day'].iloc[0]]
    return np.array(out), np.array(days)

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

f60=fetch60(); fD=fetch_day()
# 120分钟(按日对齐) -> 每天2根, 取每天最后一根(15:00)做日级信号
c120,days120=to_120_day(f60)
v120,a120=vote_and_avg(c120)
# 日级120信号: 每个day取该day两根的投票(用最后一根=下午)
df120=pd.DataFrame({"day":days120,"v120":v120,"a120":a120,"c120":c120})
df120=df120.groupby("day").last().reset_index()   # 每天用15:00那根
# 日线
cD=fD['close'].values.astype(float); vD,aD=vote_and_avg(cD)
dfD=pd.DataFrame({"day":fD['date'].values,"vD":vD,"aD":aD,"cD":cD})

m=pd.merge(dfD,df120,on="day",how="inner").reset_index(drop=True)
vD=m['vD'].values.astype(int); aD=m['aD'].values
v120=m['v120'].values.astype(int); a120=m['a120'].values
c=m['cD'].values.astype(float)   # 成交价用日线收盘(15:00)

daily_turning=(aD>np.r_[aD[0],aD[:-1]])   # 日线TRIX上升
daily_falling=(aD<np.r_[aD[0],aD[:-1]])     # 日线TRIX下降

t_daily=vD
t_120=v120
t_and=(vD==1)&(v120==1)
t_anti=(v120==1)&((vD==1)|daily_turning)

# 用户规则(状态机): 买=日线即将金叉(vD==0且TRIX上升) & 120金叉; 卖=日线即将死叉(vD==1且TRIX下降) & 120死叉
buy_ev=(vD==0)&daily_turning&(v120==1)
sell_ev=(vD==1)&daily_falling&(v120==0)
pos=0; t_user=np.zeros(len(vD),dtype=int); sw=0; prev=0
for i in range(len(vD)):
    if pos==0 and buy_ev[i]: pos=1
    elif pos==1 and sell_ev[i]: pos=0
    t_user[i]=pos
    if pos!=prev: sw+=1; prev=pos

# 不对称: 买=日线真金叉确认(vD==1); 卖=120死叉(v120==0)
pos=0; t_asym=np.zeros(len(vD),dtype=int); sw=0; prev=0
for i in range(len(vD)):
    if pos==0 and vD[i]==1: pos=1
    elif pos==1 and v120[i]==0: pos=0
    t_asym[i]=pos
    if pos!=prev: sw+=1; prev=pos

# 智能版(修正): 买/卖都锚定在 120分钟死叉(1->0), 且日线已逼近交叉(完成度>70%)
v120p=np.r_[v120[0],v120[:-1]]
gold120=(v120==1)&(v120p==0)
dead120=(v120==0)&(v120p==1)
aDp=np.r_[aD[0],aD[:-1]]
win=30
tmin=np.array([aD[max(0,i-win+1):i+1].min() for i in range(len(aD))])
tmax=np.array([aD[max(0,i-win+1):i+1].max() for i in range(len(aD))])
rng=tmax-tmin
prog_up=np.where(rng>1e-9,(aD-tmin)/rng,0.0)      # 日线TRIX在30日区间的位置(升)
prog_dn=np.where(rng>1e-9,(tmax-aD)/rng,0.0)      # 距区间顶部的回落(降)
about_gold=(vD==0)&(aD>aDp)          # 日线即将金叉(拐头向上)
about_dead=(vD==1)&(aD<aDp)          # 日线即将死叉(拐头向下)
# 最佳因果版: 日线拐头时才买120金叉 / 日线拐头时才卖120死叉
buy_smart=gold120&about_gold
sell_smart=dead120&about_dead
pos=0; t_smart=np.zeros(len(vD),dtype=int); sw=0; prev=0
for i in range(len(vD)):
    if pos==0 and buy_smart[i]: pos=1
    elif pos==1 and sell_smart[i]: pos=0
    t_smart[i]=pos
    if pos!=prev: sw+=1; prev=pos

# 理想上限: 事后定位"日线金叉前的最后一个120金叉"买 / "日线死叉前的最后一个120死叉"卖
# (决策用到未来日线交叉, 但成交在120交叉当日, 仍在日线交叉之前 -> 无未来价格)
vDp=np.r_[vD[0],vD[:-1]]
gc=np.where((vD==1)&(vDp==0))[0]
dc=np.where((vD==0)&(vDp==1))[0]
t_ideal=np.zeros(len(vD),dtype=int); buy_i=[]; sell_i=[]
for D in gc:
    js=np.where(gold120[:D])[0]
    if len(js)>0: buy_i.append(js[-1])
for D in dc:
    js=np.where(dead120[:D])[0]
    if len(js)>0: sell_i.append(js[-1])
buy_i=sorted(set(buy_i)); sell_i=sorted(set(sell_i))
sig_i=sorted(buy_i+sell_i)
pos=0; prev=0; swi=0
for i in sig_i:
    if pos==0 and i in buy_i: pos=1
    elif pos==1 and i in sell_i: pos=0
    t_ideal[i]=pos
    if pos!=prev: swi+=1; prev=pos
# 前向填充
cur=0
for i in range(len(vD)):
    if i in sig_i: cur=t_ideal[i]
    t_ideal[i]=cur

rd,mdD,sd=sim(t_daily,c)
r12,md12,s12=sim(t_120,c)
ra,mdA,sa=sim(t_and,c)
ran,mdAn,san=sim(t_anti,c)
ru,mdU,su=sim(t_user,c)
rasy,mdAsy,swasy=sim(t_asym,c)
rsm,mdSm,swSm=sim(t_smart,c)
rid,mdId,swId=sim(t_ideal,c)
hold=c[-1]/c[0]-1
# ---- 日线投票阈值敏感性: 0.5 vs 0.1 (全段2021-2026 + 当前窗口) ----
cDfull=fD['close'].values.astype(float); dates_full=fD['date'].values
vDfull05,_=vote_and_avg(cDfull,0.5)
vDfull01,_=vote_and_avg(cDfull,0.1)
w0=m['day'].iloc[0]; w1=m['day'].iloc[-1]
wmask=(dates_full>=w0)&(dates_full<=w1)
r05f,md05f,sw05f=sim(vDfull05.astype(int),cDfull)
r01f,md01f,sw01f=sim(vDfull01.astype(int),cDfull)
r01w,md01w,sw01w=sim(vDfull01[wmask].astype(int),cDfull[wmask])
print("窗口:",m['day'].iloc[0].date(),"~",m['day'].iloc[-1].date()," 共",len(m),"日")
print("\n=== 多周期混合对比 (588000, 同日15:00成交, 无7天费) ===")
print("纯日线                  : 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (rd*100,mdD*100,sd))
print("【理想】事后定位最后120交叉(上限): 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (rid*100,mdId*100,swId))
print("【智能】120金叉(日将叉)+120死叉(日将死): 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (rsm*100,mdSm*100,swSm))
print("【不对称】买日线确认+卖120死叉: 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (rasy*100,mdAsy*100,swasy))
print("混合AND(日多&120多)      : 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (ra*100,mdA*100,sa))
print("混合提前(120多&日不空)   : 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (ran*100,mdAn*100,san))
print("【用户】日将叉+120叉买/日将死+120死卖: 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (ru*100,mdU*100,su))
print("纯120分钟               : 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (r12*100,md12*100,s12))
print("买入持有                : 累计%7.1f%%" % (hold*100))
print("\n=== 日线投票阈值敏感性 (投票>阈值才持仓) ===")
print("全段2021-2026  阈值0.5: 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (r05f*100,md05f*100,sw05f))
print("全段2021-2026  阈值0.1: 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (r01f*100,md01f*100,sw01f))
print("当前窗口       阈值0.5: 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (rd*100,mdD*100,sd))
print("当前窗口       阈值0.1: 累计%7.1f%%  回撤%6.1f%%  切换%3d" % (r01w*100,md01w*100,sw01w))
print("\n=== 投票阈值扫描 (全段2021-2026 / 当前窗口2024-2026) ===")
print("%-6s %-11s %-11s %-8s %-11s %-11s %-8s"%("阈值","全段累计","全段回撤","全段切换","窗口累计","窗口回撤","窗口切换"))
for thr in [0.3,0.4,0.5,0.6,0.7]:
    v,_=vote_and_avg(cDfull,thr)
    rf,mdf,swf=sim(v.astype(int),cDfull)
    rw,mdw,sww=sim(v[wmask].astype(int),cDfull[wmask])
    print("%-6.1f %-10.1f%% %-10.1f%% %-8d %-10.1f%% %-10.1f%% %-8d"%(thr,rf*100,mdf*100,swf,rw*100,mdw*100,sww))
print("\n=== 单参数敏感度N (日线单TRIX, M=9; N越小越灵敏) ===")
print("%-5s %-11s %-11s %-8s"%("N","全段累计","全段回撤","全段切换"))
for N in [6,9,12,15,20,30]:
    tr,sig=trix_series(cDfull,N,9)
    v=(tr>sig).astype(int)
    r,md,sw=sim(v,cDfull)
    print("%-5d %-10.1f%% %-10.1f%% %-8d"%(N,r*100,md*100,sw))

def vote_from(c, combos, thr=0.5):
    trs=[]; sigs=[]
    for n,m in combos:
        tr,sig=trix_series(c,n,m); trs.append(tr); sigs.append(sig)
    pos=(np.array(trs)>np.array(sigs)).astype(int)
    return pos.mean(0)>thr

print("\n=== N向12集中: 主信号对比 (全段2021-2026 / 当前窗口2024-2026) ===")
print("%-22s %-11s %-11s %-8s %-11s %-11s %-8s"%("配置","全段累计","全段回撤","全段切换","窗口累计","窗口回撤","窗口切换"))
configs=[
    ("现状7组合(0.5)", COMBOS, 0.5),
    ("单(12,9)", [(12,9)], 0.5),
    ("单(12,12)", [(12,12)], 0.5),
    ("N12簇6个(0.5)", [(10,9),(10,12),(12,9),(12,12),(14,9),(14,12)], 0.5),
    ("N12簇6个(0.6)", [(10,9),(10,12),(12,9),(12,12),(14,9),(14,12)], 0.6),
]
for name,combos,thr in configs:
    v=vote_from(cDfull,combos,thr)
    rf,mdf,swf=sim(v.astype(int),cDfull)
    rw,mdw,sww=sim(v[wmask].astype(int),cDfull[wmask])
    print("%-22s %-10.1f%% %-10.1f%% %-8d %-10.1f%% %-10.1f%% %-8d"%(
        name, rf*100, mdf*100, swf, rw*100, mdw*100, sww))

