"""共用的小格式化工具，給開倉分頁的主畫面跟分析詳細視窗一起用。"""

AMP_LABEL = {"high": "高", "low": "低"}
VOL_LABEL = {"high": "高", "low": "低"}


def format_history_item(record: dict) -> str:
    return f"{record.get('timestamp', '')}　{record.get('strategy', '')}"


def format_summary(record: dict) -> str:
    amp = AMP_LABEL.get(record.get("amplitude"), record.get("amplitude"))
    vol = VOL_LABEL.get(record.get("volatility"), record.get("volatility"))
    return (
        f"時間：{record.get('timestamp', '')}　"
        f"振幅：{amp}　波動率：{vol}　建議策略：{record.get('strategy', '')}"
    )
