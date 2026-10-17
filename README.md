# market-radar

AI链强弱雷达 dashboard(自动生成,仅市场分析,无个人数据)。源仓库私有。

- AI 链强弱雷达：https://cloudyu0430.github.io/market-radar/
- Crypto 标的池（每日扫描，不含个人持仓）：https://cloudyu0430.github.io/market-radar/crypto/

## 工具

- `tools/lowcap_oi_study.py`：Binance U 本位永续「低市值 + 高 OI」拉盘事件统计（回撤深度、二次拉升概率、OI 撤退与资金费率信号、50% 回撤接多的止损方案回测）。纯标准库，本地运行：

  ```bash
  python3 tools/lowcap_oi_study.py                    # 默认：近 30 天，24h 涨幅 ≥40%，OI/市值 ≥30%
  python3 tools/lowcap_oi_study.py --pump 0.3 --oi-mcap 0.2
  CG_API_KEY=xxx python3 tools/lowcap_oi_study.py     # 可选：CoinGecko Demo key，减少限流
  ```

  输出在 `oi_study_out/`（已 gitignore）。Binance 合约接口对美国 IP 返回 451，GitHub Actions 托管 runner 无法使用，需在可访问的网络下跑；建议每 1–2 周一次，让 `events_master.csv` 累积样本。
