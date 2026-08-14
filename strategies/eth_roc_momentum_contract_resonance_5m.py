"""ETH 双ROC动量策略 — 合约共振版 5m 适配参数

继承 EthROCMomentumContractResonance (五维共振逻辑完全复用), 仅放大 ROC 周期:
  ROC_SHORT = 40    (1h 的 8  → 5m 放大 5 倍)
  ROC_MEDIUM = 100  (1h 的 20 → 5m 放大 5 倍)
  ROC_LONG   = 250  (1h 的 50 → 5m 放大 5 倍)

其余时间尺度参数保持 5m 原值(用户暂未要求调整):
  VOL_MA_PERIOD=20 / TREND_MA_PERIOD=50 / ATR_STATE_PERIOD=50 / MAX_HOLD_BARS=72
  (如需一并放大可在此覆盖, 与 live_trader_contract.py 参数无关)
"""
from strategies.eth_roc_momentum_contract_resonance import EthROCMomentumContractResonance


class EthROCMomentumContractResonance5m(EthROCMomentumContractResonance):
    name = "ETH双ROC共振策略-合约20x·5m适配"

    ROC_SHORT = 40
    ROC_MEDIUM = 100
    ROC_LONG = 250
