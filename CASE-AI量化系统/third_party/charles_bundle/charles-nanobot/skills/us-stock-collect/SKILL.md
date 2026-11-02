---
name: us-stock-collect
description: "采集美股日线并写入 MySQL trade_stock_us_daily。在用户要求更新美股行情、入库美股K线、采集 COHR 等美股数据时使用。"
keywords: 美股, 数据采集, 日线, Yahoo, COHR, 入库
---

# 美股日线采集

## 适用场景

- 把一只或多只美股的日线写入本地库 `trade_stock_us_daily`
- 把库里的美股行情补到最新交易日
- A 股行情不要用本技能，继续用 stock-price

## 命令

在 Charles 工作目录执行。代码可写 `COHR` 或 `COHR.US`，默认近 5 年：

```
python skills/us-stock-collect/scripts/collect_us_daily.py --symbols COHR
```

多只、指定区间：

```
python skills/us-stock-collect/scripts/collect_us_daily.py --symbols COHR,NVDA --start 2024-01-01 --end 2026-11-02
```

课程清单里的全部美股很慢，只有用户明确要求全量时才执行：

```
python skills/us-stock-collect/scripts/collect_us_daily.py --all
```

## 执行后

读最后一行 `RESULT_JSON`，报告每只代码是否写入成功、库内条数和日期范围。Yahoo 没有数据时不要说已入库。
