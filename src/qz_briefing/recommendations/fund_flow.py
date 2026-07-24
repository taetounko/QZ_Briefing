"""Pure investor-flow feature extraction and 0-25 scoring."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable

@dataclass(frozen=True)
class FundFlowScoringConfig:
    amount_unit_multiplier: float=1.; foreign_5d_full_ratio: float=.03; foreign_20d_full_ratio: float=.08
    institution_5d_full_ratio: float=.03; institution_20d_full_ratio: float=.08; joint_full_ratio: float=.02
    weights: tuple[float,...]=(5,4,5,4,3,2,2)

@dataclass(frozen=True)
class FundFlowFeatures:
    foreign_net_5d: float|None; foreign_net_20d: float|None; institution_net_5d: float|None; institution_net_20d: float|None
    foreign_buy_days_5d: int; institution_buy_days_5d: int; foreign_normalized_5d: float|None; foreign_normalized_20d: float|None
    institution_normalized_5d: float|None; institution_normalized_20d: float|None; joint_buy_5d: bool; flow_acceleration: float|None
    fund_flow_score: float; fund_flow_reasons: tuple[str,...]; fund_flow_status: str

def parse_signed_number(value:object)->float|None:
    text="" if value is None else str(value).strip().replace(",","")
    if not text:return None
    try:return float(text)
    except (TypeError,ValueError):return None
def _series(values:Iterable[object])->list[float|None]:return [parse_signed_number(v) for v in values]
def _sum(values:list[float|None],period:int)->float|None:
    part=values[-period:]
    return sum(v for v in part if v is not None) if len(part)==period and all(v is not None for v in part) else None
def _ratio(value:float|None,full:float)->float:return min(1.,max(0.,(value or 0)/full)) if full>0 else 0.

def compute_fund_flow_score(foreign_values:Iterable[object],institution_values:Iterable[object],average_trading_value_20d:object,config:FundFlowScoringConfig|None=None)->FundFlowFeatures:
    c=config or FundFlowScoringConfig(); f=_series(foreign_values); i=_series(institution_values); available=min(len(f),len(i))
    status="data_unavailable" if available==0 else "complete" if available>=20 else "partial"; f5,f20,i5,i20=_sum(f,5),_sum(f,20),_sum(i,5),_sum(i,20)
    average=parse_signed_number(average_trading_value_20d); denominator=average if average and average>0 else None
    def norm(v):return v*c.amount_unit_multiplier/denominator if v is not None and denominator else None
    fn5,fn20,in5,in20=map(norm,(f5,f20,i5,i20)); fdays=sum(v is not None and v>0 for v in f[-5:]); idays=sum(v is not None and v>0 for v in i[-5:])
    joint=bool(f5 is not None and i5 is not None and f5>0 and i5>0); acceleration=None
    if available>=20:
        recent=[(f[x] or 0)+(i[x] or 0) for x in range(-5,0)]; prior=[(f[x] or 0)+(i[x] or 0) for x in range(-20,-5)]
        raw=sum(recent)/5-sum(prior)/15; acceleration=raw*c.amount_unit_multiplier/denominator if denominator else raw
    w=c.weights; score=_ratio(fn5,c.foreign_5d_full_ratio)*w[0]+_ratio(fn20,c.foreign_20d_full_ratio)*w[1]+_ratio(in5,c.institution_5d_full_ratio)*w[2]+_ratio(in20,c.institution_20d_full_ratio)*w[3]
    if joint:score+=_ratio(min(fn5 or 0,in5 or 0),c.joint_full_ratio)*w[4]
    score+=(fdays+idays)/10*w[5]
    if acceleration is not None and acceleration>0:score+=min(1.,acceleration/.02)*w[6]
    score=round(min(25.,max(0.,score)),2); reasons=[]
    if f5 is not None and f5>0:reasons.append(f"외국인 최근 5일 순매수 {f5:,.0f}")
    if i5 is not None and i5>0:reasons.append(f"기관 최근 5일 순매수 {i5:,.0f}")
    if joint:reasons.append("외국인·기관 최근 5일 동반 순매수")
    if acceleration is not None and acceleration>0:reasons.append("최근 5일 수급이 이전 15일보다 개선")
    if status!="complete":reasons.append("수급 자료 부족" if status=="data_unavailable" else "수급 자료 일부만 확인")
    return FundFlowFeatures(f5,f20,i5,i20,fdays,idays,fn5,fn20,in5,in20,joint,acceleration,score,tuple(reasons),status)
