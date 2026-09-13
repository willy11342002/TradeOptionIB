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
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.services.background_tasks import run_blocking

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


class ScanCodeSuggestions(BaseModel):
    codes: list[str] = Field(
        description="從候選清單中依相關性排序挑最相關的掃描代碼，最多8個，只能填候選清單裡真實存在的 code"
    )


_SCAN_CODE_SYSTEM_PROMPT = (
    "你是美股選擇權交易的市場掃描小幫手。使用者會用自然語言或關鍵字片段"
    "描述想找的股票特徵，你要從提供的候選「掃描代碼」清單中，依相關性由"
    "高到低挑出最符合的代碼，最多 8 個。只能填候選清單裡真實存在的 code "
    "字串，不要自己發明或修改代碼拼字，也不要輸出候選清單以外的代碼。"
)


def _require_openrouter_config() -> str:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY 未設定，請確認 .env 檔")
    model_name = os.environ.get("OPENROUTER_MODEL")
    if not model_name:
        raise RuntimeError("OPENROUTER_MODEL 未設定，請確認 .env 檔")
    return model_name


def _suggest_scan_codes_sync(query: str, candidates: list, model_name: str) -> list[str]:
    """*** 只能在背景執行緒(run_in_executor)裡呼叫，這裡面絕對不能碰任
    何 Qt 物件 ***：這支函式純粹是「準備 prompt→呼叫 Agent→回傳資料」，
    沒有 self.xxx_widget 這種東西，回傳值是單純的 list[str]——線程安全
    性靠的不是「小心不要碰 UI」這種自律，是結構上這支函式的作用域裡根
    本沒有任何 Qt widget 的參照可以碰。"""
    from pydantic_ai import Agent
    from pydantic_ai.models.openrouter import OpenRouterModelSettings

    agent = Agent(
        f"openrouter:{model_name}",
        output_type=ScanCodeSuggestions,
        system_prompt=_SCAN_CODE_SYSTEM_PROMPT,
        model_settings=OpenRouterModelSettings(max_tokens=300),
    )
    table = "\n".join(f"{c.code}: {c.name_zh or c.name_en}" for c in candidates)
    prompt = f"候選掃描代碼清單：\n{table}\n\n使用者輸入：{query}"
    result = agent.run_sync(prompt)
    return result.output.codes


async def suggest_scan_codes(query: str, candidates: list) -> list[str]:
    """讓 AI 從實際存在的 IB 掃描代碼清單(candidates: list[ScanTypeDef])
    裡挑出最接近使用者輸入的幾個，給 app/views/scan_code_picker.py 的
    debounce 搜尋框用。

    *** 一定要用 loop.run_in_executor() 丟到背景執行緒，不能直接
    await agent.run(...) ***：實測回報過直接掛在主執行緒的 qasync 迴圈
    上呼叫時，畫面會明顯卡頓——pydantic-ai/httpx 準備 request(組 JSON
    schema、序列化 prompt)、解析 response(pydantic 驗證)這些都是
    CPU-bound 的同步工作，就算包在 async 函式裡、用 await 呼叫，這些同
    步片段還是會佔用主執行緒，Qt 的訊息迴圈在這些片段執行期間沒辦法處理
    畫面重繪/滑鼠鍵盤事件，使用者就會感覺到卡頓。

    (沿革記錄：曾經因為懷疑另一個問題(見下方 *** 執行緒安全性 ***)而把
    這段改回主執行緒直接 await，後來確認那個懷疑的根因跟這裡無關(是
    scan_code_picker.py 同步路徑上的另一個問題，已經修掉)，所以重新改
    回 run_in_executor()，只是為了修這裡回報的卡頓，不是因為擔心崩潰。)

    *** 執行緒安全性(PyQt 的 method 大多不是 thread-safe 的，混用會直接
    corrupt 記憶體，不是慢而已) ***：
    1. `_suggest_scan_codes_sync()` 在背景執行緒裡執行，裡面完全沒有任
       何 Qt widget 的參照，純粹處理 Agent/字串/list，不可能在錯的執行
       緒碰到 UI。
    2. `await loop.run_in_executor(...)` 這一行本身是 asyncio 的標準保
       證：不管背景執行緒做了什麼，這個 await 完成後，這支 coroutine
       (包含它 `await` 完成後接下來的每一行)一定會被排程回「驅動這個
       事件迴圈的那個執行緒」繼續執行——這支 app 裡驅動 qasync 事件迴圈
       的就是主執行緒(唯一一個)，所以 await 之後接觸
       app/views/scan_code_picker.py 的 self.dropdown/self.ai_status_label
       這些 Qt widget 保證都在主執行緒，不會有 cross-thread 呼叫。
    這兩點合起來，才是「丟到背景執行緒沒有引入執行緒安全性問題」的完整
    理由，不是「應該沒事」這種猜測。

    缺 API key／呼叫失敗都直接把例外往上丟，由呼叫端決定要怎麼降級顯示
    (安靜維持本地比對結果，不彈窗、不洗畫面)。"""
    model_name = _require_openrouter_config()

    codes = await run_blocking(_suggest_scan_codes_sync, query, candidates, model_name)

    valid = {c.code for c in candidates}
    return [code for code in codes if code in valid][:8]


class ProposedFilterValue(BaseModel):
    filter_id: str = Field(description="必須是候選篩選條件清單裡真實存在的 id")
    above: Optional[float] = Field(default=None, description="下限值，不需要就留 null")
    below: Optional[float] = Field(default=None, description="上限值，不需要就留 null")


class FilterProposal(BaseModel):
    scan_code: str = Field(description="必須是候選掃描代碼清單裡真實存在的 code")
    filters: list[ProposedFilterValue] = Field(default_factory=list)
    rationale: str = Field(description="一兩句話說明理由，繁體中文")


_FILTER_ASSISTANT_SYSTEM_PROMPT = (
    "你是美股選擇權交易的市場篩選小幫手。使用者會用自然語言描述想篩選的"
    "股票特徵，你要從提供的候選「掃描代碼」清單挑一個最符合的當基礎排序"
    "方式，再從候選「篩選條件」清單挑幾個相關的、給出合理的上下限數值。"
    "只能填候選清單裡真實存在的 scan_code / filter_id，不要自己發明。"
    "*** 每個篩選條件後面括號標的「單位」一定要換算，不能照使用者講的"
    "原始數字直接填 ***：例如單位是「M」代表這個欄位要填「以百萬美元為"
    "單位」的數字，使用者說「市值大於10億美元」，正確填的是 1000(=10億"
    "÷100萬)，不是 1000000000；單位是「%」的欄位，使用者說「30%」就填 "
    "30，不是 0.3。每個篩選條件的 above/below 依欄位語意給合理數字，不"
    "需要的一邊留 null 即可，不用兩邊都填。rationale 請用一兩句繁體中文"
    "簡短說明你的選擇理由。"
)


def _suggest_filters_sync(
    user_text: str, filter_catalog: list, scan_type_catalog: list, model_name: str,
) -> FilterProposal:
    """*** 只能在背景執行緒(run_in_executor)裡呼叫，理由跟
    _suggest_scan_codes_sync() 開頭的說明一樣：這支函式作用域裡沒有任何
    Qt widget 的參照，結構上不可能在錯的執行緒碰到 UI。***"""
    from pydantic_ai import Agent
    from pydantic_ai.models.openrouter import OpenRouterModelSettings

    agent = Agent(
        f"openrouter:{model_name}",
        output_type=FilterProposal,
        system_prompt=_FILTER_ASSISTANT_SYSTEM_PROMPT,
        # 一定要明確設 max_tokens——踩過的實測結論：pydantic-ai 沒指定時
        # 會自己算一個很大的預設值，部分 OpenRouter 供應商路由(例如這裡
        # 用的 moonshotai/kimi-k2 走 Novita)對輸出長度另有更低的硬上限，
        # 會直接回 400 "max_tokens exceeds maximum"。這個輸出結構(掃描
        # 代碼+理由+最多十來個篩選條件)用不到 2000 tokens。
        model_settings=OpenRouterModelSettings(max_tokens=2000),
    )
    scan_table = "\n".join(f"{c.code}: {c.name_zh or c.name_en}" for c in scan_type_catalog)
    # *** 一定要把單位(unit_zh)告訴 AI，不能只給名稱 ***：踩過的實測結
    # 論——IB 有些欄位的 TagValue 是用特殊比例儲存的(例如市值篩選欄位
    # marketCapAbove1e6 是「以百萬美元為單位」，使用者說「10億美元」時
    # 正確的填值是 1000，不是 1000000000)，AI 沒被告知這件事就會照字面
    # 上的美元金額填，數字會差一千倍。
    filter_table = "\n".join(
        f"{f.id}: {f.category_zh}／{f.label_zh}（單位：{f.fields[0].unit_zh or '無單位'}）"
        for f in filter_catalog
    )
    prompt = (
        f"候選掃描代碼清單：\n{scan_table}\n\n"
        f"候選篩選條件清單(格式 id: 分類／名稱)：\n{filter_table}\n\n"
        f"使用者描述：{user_text}"
    )
    result = agent.run_sync(prompt)
    return result.output


async def suggest_filters(user_text: str, filter_catalog: list, scan_type_catalog: list) -> FilterProposal:
    """filter_catalog: list[FilterDef]，scan_type_catalog: list[ScanTypeDef]。

    一樣要用 loop.run_in_executor() 丟到背景執行緒——理由(修卡頓)跟執行
    緒安全性的完整論證見 suggest_scan_codes() 開頭的說明，這裡不重複。

    回傳前一定要驗證 AI 給的 scan_code/filter_id 是不是真的存在於候選
    清單，不存在的 filter 直接濾掉、scan_code 不存在就清空成空字串，呼
    叫端(FilterAssistantDialog)看到空字串就不預填掃描代碼——不能盲目相
    信 AI 輸出的字串一定合法。"""
    model_name = _require_openrouter_config()

    proposal = await run_blocking(_suggest_filters_sync, user_text, filter_catalog, scan_type_catalog, model_name)

    valid_scan_codes = {c.code for c in scan_type_catalog}
    valid_filter_ids = {f.id for f in filter_catalog}
    proposal.filters = [f for f in proposal.filters if f.filter_id in valid_filter_ids]
    if proposal.scan_code not in valid_scan_codes:
        proposal.scan_code = ""
    return proposal


class _TermTranslation(BaseModel):
    term: str = Field(description="必須是候選清單裡原封不動的英文字串，一字不改(含大小寫/標點)")
    zh: str = Field(description="簡短繁體中文翻譯，金融業界慣用說法")


class _TermTranslationBatch(BaseModel):
    items: list[_TermTranslation]


_TERM_TRANSLATE_SYSTEM_PROMPT = (
    "你在幫一個美股/ETF選擇權交易app翻譯Interactive Brokers回傳的公司產業"
    "(industry)/類別(category)分類字串成繁體中文，簡短(通常2-6個字)、"
    "金融業界慣用的說法，不要逐字直譯。例如 \"Consumer, Non-cyclical\"→"
    "\"民生消費\"、\"Pharmaceuticals\"→\"製藥\"、\"Biotechnology\"→\"生技\"、"
    "\"Technology\"→\"科技\"、\"Commercial Services\"→\"商業服務\"。"
    "term 欄位必須照候選清單裡實際給的原始字串填，一字不改，不要跳過任"
    "何一筆。"
)


def _translate_terms_sync(terms: list[str], model_name: str) -> dict[str, str]:
    """*** 只能在背景執行緒(run_in_executor)裡呼叫，理由跟
    _suggest_scan_codes_sync() 開頭的說明一樣。***"""
    from pydantic_ai import Agent
    from pydantic_ai.models.openrouter import OpenRouterModelSettings

    agent = Agent(
        f"openrouter:{model_name}",
        output_type=_TermTranslationBatch,
        system_prompt=_TERM_TRANSLATE_SYSTEM_PROMPT,
        model_settings=OpenRouterModelSettings(max_tokens=2000),
    )
    prompt = "請翻譯以下字串：\n" + "\n".join(terms)
    result = agent.run_sync(prompt)
    return {item.term: item.zh for item in result.output.items}


async def translate_terms(terms: list[str]) -> dict[str, str]:
    """把 terms(產業/類別英文分類字串)丟給 AI 翻譯成繁體中文，回傳
    {原文: 中文}——翻不出來的字串不會出現在回傳的 dict 裡。給
    app/services/industry_translations.py 的快取補新字串用，缺 API
    key／呼叫失敗都安靜回傳空 dict，不往上丟例外——這是候選清單顯示的
    輔助功能，不該讓整個候選清單因為翻譯失敗而顯示不出來，呼叫端退回顯
    示英文原文即可，不需要特別處理錯誤。"""
    if not terms:
        return {}
    try:
        model_name = _require_openrouter_config()
        return await run_blocking(_translate_terms_sync, terms, model_name)
    except Exception:  # noqa: BLE001
        return {}
