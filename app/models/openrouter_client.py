"""
用 pydantic-ai 呼叫 OpenRouter，讓框架負責把輸出強制轉成結構化的
QuadrantJudgment（振幅/波動率高低判斷、壓力/支撐價位、Markdown格式理由），
不用再自己刻 JSON 格式的 prompt 規則跟手動 regex 解析。同時用 pydantic-ai
的原生 web search 工具讓 LLM 自己查最近的台股新聞/利多利空——FinMind 的
新聞 dataset 實測是付費限定，免費帳號打不到，市場消息這塊改成讓 LLM 自己
上網查，不再依賴額外的新聞資料源。

Model 名稱刻意不寫死，讀 os.environ["OPENROUTER_MODEL"]，使用者換模型只要
改 .env，不用動程式碼 (用 pydantic-ai 的 "openrouter:<model>" 字串型
model 寫法；OPENROUTER_API_KEY 環境變數 pydantic-ai 自己會讀，不用手動
組 headers)。象限對應到哪個策略原本是程式碼裡的固定規則，寫在已經隨
群益台指選擇權功能一起刪除的 app/services/opening_analysis.py 裡
(STRATEGY_MAP)——這支模組目前沒有任何地方呼叫，內容(prompt/策略對應)
還是台指選擇權導向，日後如果要重新接上新功能，要先決定策略對應規則搬
去哪裡、prompt 要不要改成美股選擇權導向。
"""
import json
import os
from typing import Literal

from pydantic import BaseModel, Field

# pydantic_ai 本身匯入很重 (拉一堆 httpx/anyio/opentelemetry 之類的東西)，
# 之前放在檔案最上面害整支程式一啟動就要載入，明明使用者可能根本還沒點
# 「分析」。改成只在真的要呼叫 LLM 的時候 (judge_quadrant 執行時) 才匯入。


class QuadrantJudgment(BaseModel):
    amplitude: Literal["high", "low"] = Field(description="未來一週台股加權指數振幅(以%衡量)是高是低")
    volatility: Literal["high", "low"] = Field(
        description="未來一週波動率(選擇權隱含波動的定價水準)是高是低"
    )
    resistance_levels: list[float] = Field(
        description="未來一週潛在壓力價位(指數點位)，由近到遠排序，抓最關鍵的 1-3 個即可"
    )
    support_levels: list[float] = Field(
        description="未來一週潛在支撐價位(指數點位)，由近到遠排序，抓最關鍵的 1-3 個即可"
    )
    reasoning: str = Field(description="Markdown 格式的完整判斷理由，繁體中文")


SYSTEM_PROMPT = (
    "你是台指選擇權交易的市場分析助手。請先用網路搜尋工具查一下最近幾天"
    "台股/台指相關的新聞、市場上已知的利多利空消息，再綜合使用者提供的"
    "原始資料（未來一週財經日曆、近期台股加權指數每日價格、近期選擇權"
    "波動率指數走勢、最近一個交易日的選擇權籌碼彙總(OI/成交量)），"
    "質化判斷未來一週台股的「振幅」(amplitude，指數可能的波動區間大小) "
    "跟「波動率」(volatility，市場對未來不確定性的定價水準) 各是 high 還是 low。\n"
    "特別注意：台股加權指數目前點位在 4 萬多點，指數基期高，同樣漲跌 1% "
    "換算成點數就有四、五百點，單看絕對點數會顯得波動很大，但實際漲跌幅"
    "不一定真的高。判斷振幅／波動率時「一定要用百分比」，不能只看點數。"
    "使用者提供的每日價格資料裡每一天都已經算好 change_pct（較前一日收盤"
    "的漲跌幅%）跟 range_pct（當日最高最低價差相對前一日收盤的%），請以"
    "這兩個百分比欄位為主要判斷依據，理由裡也請用百分比描述，不要只講點數。"
    "選擇權波動率指數是市場對未來波動的定價水準，可以直接參考其近期水位"
    "高低與趨勢。\n"
    "另外請根據選擇權籌碼分布（call/put 未平倉量最大的履約價、Put/Call "
    "未平倉比率）、近期價格區間、波動率水位，判斷出未來一週最可能的壓力"
    "價位(resistance_levels)跟支撐價位(support_levels)，履約價 OI 密集"
    "的位置通常是重要參考，由近到遠排序，抓最關鍵的 1-3 個即可，不用列"
    "太多。\n"
    "reasoning 欄位請用 Markdown 格式撰寫，方便畫面上分段閱讀：用「## 」"
    "區分幾個小節（例如財經事件、價格走勢、波動率、籌碼、壓力支撐、"
    "結論），關鍵的百分比數字、價位、判斷結論用 **粗體** 標出，多個因素用"
    "條列(- )列出，不要整段擠在一起。"
)


def _build_user_prompt(
    calendar_events: list[dict],
    price_history: list[dict],
    vix_history: list[dict],
    option_chain_summary: dict,
) -> str:
    return (
        "【未來一週財經日曆】\n"
        f"{json.dumps(calendar_events, ensure_ascii=False)}\n\n"
        "【近期台股加權指數(TAIEX)每日價格】\n"
        f"{json.dumps(price_history, ensure_ascii=False)}\n\n"
        "【近期台指選擇權波動率指數】\n"
        f"{json.dumps(vix_history, ensure_ascii=False)}\n\n"
        "【最近一個交易日的選擇權籌碼分布彙總(前面成交量最大的合約月份，"
        "call/put 未平倉量最大的履約價、整體 Put/Call 未平倉比率)】\n"
        f"{json.dumps(option_chain_summary, ensure_ascii=False)}\n"
    )


def judge_quadrant(
    calendar_events: list[dict],
    price_history: list[dict],
    vix_history: list[dict],
    option_chain_summary: dict,
) -> QuadrantJudgment:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY 未設定，請確認 .env 檔")
    model_name = os.environ.get("OPENROUTER_MODEL")
    if not model_name:
        raise RuntimeError("OPENROUTER_MODEL 未設定，請確認 .env 檔")

    from pydantic_ai import Agent
    from pydantic_ai.capabilities import NativeTool
    from pydantic_ai.models.openrouter import OpenRouterModelSettings
    from pydantic_ai.native_tools import WebSearchTool

    agent = Agent(
        f"openrouter:{model_name}",
        output_type=QuadrantJudgment,
        system_prompt=SYSTEM_PROMPT,
        capabilities=[NativeTool(WebSearchTool())],
        model_settings=OpenRouterModelSettings(max_tokens=4000),
    )
    result = agent.run_sync(
        _build_user_prompt(calendar_events, price_history, vix_history, option_chain_summary)
    )
    return result.output
