"""Deterministic advanced BI helpers used by Auto Dashboard.
All outputs are computed from the uploaded dataframe; no business fact is fabricated.
"""
import math
import numpy as np
import pandas as pd


def advanced_insights(df: pd.DataFrame, numeric: list[str], dates: list[str], categorical: list[str]):
    findings=[]; facts=[]; warnings=[]
    # Robust anomaly detection using IQR, capped so a dashboard remains readable.
    for col in numeric[:3]:
        s=pd.to_numeric(df[col], errors='coerce').dropna()
        if len(s)<8: continue
        q1,q3=s.quantile(.25),s.quantile(.75); iqr=q3-q1
        if iqr<=0: continue
        lo,hi=q1-1.5*iqr,q3+1.5*iqr
        idx=s[(s<lo)|(s>hi)]
        if len(idx):
            findings.append({"text":f"{col}: {len(idx)} potential outlier(s) detected using the IQR rule (bounds {lo:.2f} to {hi:.2f}).","fact_ids":[]})
            facts.append((f"Potential anomalies in {col}", [{"labels": {col:"outlier_count"}, "value":int(len(idx))}], "bar", {"method":"IQR","column":col,"lower":float(lo),"upper":float(hi)}))
        break
    # Trend comparison when a date and numeric measure exist.
    if dates and numeric:
        dcol,ncol=dates[0],numeric[0]
        t=df[[dcol,ncol]].copy(); t[dcol]=pd.to_datetime(t[dcol],errors='coerce'); t[ncol]=pd.to_numeric(t[ncol],errors='coerce'); t=t.dropna()
        if len(t)>=3:
            g=t.groupby(t[dcol].dt.to_period('M'))[ncol].sum().sort_index()
            if len(g)>=2:
                first,last=float(g.iloc[0]),float(g.iloc[-1])
                if first!=0:
                    pct=(last-first)/abs(first)*100
                    direction='increased' if pct>0 else 'decreased'
                    findings.append({"text":f"{ncol} {direction} by {abs(pct):.1f}% between the first and latest available monthly periods.","fact_ids":[]})
    # Simple concentration / segmentation signal.
    if categorical and numeric:
        c,n=categorical[0],numeric[0]
        g=df.assign(__v=pd.to_numeric(df[n],errors='coerce')).groupby(c,dropna=False)['__v'].sum().dropna().sort_values(ascending=False)
        if len(g)>1 and g.sum()!=0:
            share=float(g.iloc[0]/g.sum()*100)
            findings.append({"text":f"The largest {c} segment contributes {share:.1f}% of the measured {n} total, indicating {'high' if share>=50 else 'moderate'} concentration.","fact_ids":[]})
    return facts,findings,warnings


def executive_summary(df, facts, profile, max_items=5):
    cols=profile.get('columns',[]); missing=sum(int(c.get('missing',0) or 0) for c in cols)
    lines=[f"Dataset contains {len(df):,} records across {len(cols)} columns."]
    if missing: lines.append(f"Data quality note: {missing:,} missing cell value(s) were identified during profiling.")
    kpis=[f for f in facts if f.get('chart')=='kpi'][:3]
    for f in kpis:
        v=f.get('rows',[{}])[0].get('value');
        if v is not None: lines.append(f"{f['description']}: {float(v):,.2f}" if isinstance(v,float) else f"{f['description']}: {v:,}")
    return lines[:max_items]
