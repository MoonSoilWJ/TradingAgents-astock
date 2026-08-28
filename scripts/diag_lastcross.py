"""诊断: 日线金叉前真正的'最后一个120金叉'在哪, 以及当前买点落在哪."""
import numpy as np, pandas as pd
from compare_hybrid import fetch60, fetch_day, vote_and_avg

def main():
    f60=fetch60(); fD=fetch_day()
    c120=f60['close'].values.astype(float)
    v120,a120=vote_and_avg(c120)
    cD=fD['close'].values.astype(float); vD,aD=vote_and_avg(cD)
    dfD=pd.DataFrame({"day":fD['date'].values,"vD":vD,"aD":aD})
    df120=pd.DataFrame({"day":f60['date'].values,"v120":v120})
    m=pd.merge(dfD,df120,on="day",how="inner").reset_index(drop=True)
    vD=m['vD'].values.astype(int); aD=m['aD'].values
    v120=m['v120'].values.astype(int); day=m['day'].values
    N=len(vD)
    vDp=np.r_[vD[0],vD[:-1]]
    v120p=np.r_[v120[0],v120[:-1]]
    gold120=(v120==1)&(v120p==0)
    dead120=(v120==0)&(v120p==1)
    gc=np.where((vD==1)&(vDp==0))[0]      # 日线金叉
    dc=np.where((vD==0)&(vDp==1))[0]      # 日线死叉
    # 当前买(宽): gold120 & vD==0 & aD升
    aDp=np.r_[aD[0],aD[:-1]]
    about_gold=(vD==0)&(aD>aDp)
    buy_broad=gold120&about_gold
    win=30
    tmin=np.array([aD[max(0,i-win+1):i+1].min() for i in range(N)])
    tmax=np.array([aD[max(0,i-win+1):i+1].max() for i in range(N)])
    rng=tmax-tmin
    prog_up=np.where(rng>1e-9,(aD-tmin)/rng,0.0)
    print("== 日线金叉(gc) 前的最后一个120金叉 vs 当前买点 ==")
    print("%-12s %-12s %-8s %-12s %-8s"%("日线金叉日","理想买(最后120金叉)","prog","当前买点","prog"))
    def ds(x):
        return str(x)[:10] if x is not None else 'None'
    for D in gc:
        js=np.where(gold120[:D])[0]
        j=js[-1] if len(js)>0 else None
        kb=np.where(buy_broad[:D])[0]
        k=kb[-1] if len(kb)>0 else None
        print("%-12s %-12s %-8.2f %-12s %-8.2f"%(ds(day[D]),ds(day[j]),
              prog_up[j] if j is not None else 0, ds(day[k]), prog_up[k] if k is not None else 0))
    print("\n== 日线死叉(dc) 前的最后一个120死叉 ==")
    print("%-12s %-12s"%("日线死叉日","理想卖(最后120死叉)"))
    for D in dc:
        js=np.where(dead120[:D])[0]
        j=js[-1] if len(js)>0 else None
        print("%-12s %-12s"%(ds(day[D]),ds(day[j])))

if __name__=="__main__":
    main()
