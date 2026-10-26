"""
天気情報取得ツール — 気象庁 API ラッパー

気象庁の公開 API (https://www.jma.go.jp/bosai/forecast/) を利用して
指定地域の天気予報を取得する。

Nova 2.5 Sonic の Tool use で呼び出されることを想定。
"""

import asyncio
import json
import urllib.request


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "AWS-Deep-Cuts-Handson/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))

# ─── Tool Spec (Nova 2.5 Sonic に渡すツール定義) ─────────────────
WEATHER_TOOL_SPEC = {
    "toolSpec": {
        "name": "get_weather",
        "description": (
            "Get today's weather forecast for a location in Japan. "
            "Call this when the user asks about the weather. "
            "Returns weather description and temperature."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "area": {
                        "type": "string",
                        "description": (
                            "Area name in Japanese (e.g. '東京', '大阪', '福岡'). "
                            "If unknown, use '東京'."
                        ),
                    },
                },
                "required": ["area"],
            }
        },
    }
}

# 気象庁 API の地域コード (主要都市のみ)
AREA_CODES = {
    "北海道": "016000",
    "札幌": "016000",
    "青森": "020000",
    "岩手": "030000",
    "宮城": "040000",
    "仙台": "040000",
    "秋田": "050000",
    "山形": "060000",
    "福島": "070000",
    "茨城": "080000",
    "栃木": "090000",
    "群馬": "100000",
    "埼玉": "110000",
    "千葉": "120000",
    "東京": "130000",
    "神奈川": "140000",
    "横浜": "140000",
    "新潟": "150000",
    "富山": "160000",
    "石川": "170000",
    "福井": "180000",
    "山梨": "190000",
    "長野": "200000",
    "岐阜": "210000",
    "静岡": "220000",
    "愛知": "230000",
    "名古屋": "230000",
    "三重": "240000",
    "滋賀": "250000",
    "京都": "260000",
    "大阪": "270000",
    "兵庫": "280000",
    "神戸": "280000",
    "奈良": "290000",
    "和歌山": "300000",
    "鳥取": "310000",
    "島根": "320000",
    "岡山": "330000",
    "広島": "340000",
    "山口": "350000",
    "徳島": "360000",
    "香川": "370000",
    "愛媛": "380000",
    "高知": "390000",
    "福岡": "400000",
    "佐賀": "410000",
    "長崎": "420000",
    "熊本": "430000",
    "大分": "440000",
    "宮崎": "450000",
    "鹿児島": "460100",
    "沖縄": "471000",
    "那覇": "471000",
}


def _format_temps(time_defines: list, temps: list) -> str:
    """気象庁の気温予報を「最低/最高」の文字列にする

    timeDefines の 00:00 が朝の最低気温、09:00 が日中の最高気温を表す。
    朝の発表では当日の最低気温が過ぎているため、00:00 にも最高気温と同じ値が入る。
    """
    parts = []
    first_date = None
    for time_define, temp in zip(time_defines, temps):
        if not temp:
            continue
        date, _, clock = time_define.partition("T")
        first_date = first_date or date
        label = "最高" if clock.startswith("09") else "最低"
        parts.append((date, label, temp))
    # 最初の日付 (発表時刻により今日または明日) の値だけを返す
    day = [(label, temp) for date, label, temp in parts if date == first_date]
    if len(day) == 2 and day[0][1] == day[1][1]:
        day = [("最高", day[0][1])]
    return " / ".join(f"{label}{temp}℃" for label, temp in day)


async def get_weather(params: dict) -> dict:
    """気象庁 API から天気予報を取得"""
    area = params.get("area", "東京")

    # 地域コード解決
    area_code = AREA_CODES.get(area)
    if not area_code:
        # 部分一致を試す
        for key, code in AREA_CODES.items():
            if key in area or area in key:
                area_code = code
                area = key
                break

    note = None
    if not area_code:
        # 対応表に無い地域は東京で代替し、その旨をモデルに伝える
        note = f"'{area}' の地域コードが見つからなかったため、東京の天気を返します。"
        area_code = "130000"
        area = "東京"

    # 気象庁 API 呼び出し
    url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json"

    try:
        # urllib は同期 I/O のため、イベントループ (音声の中継) を止めないようスレッドで実行
        data = await asyncio.to_thread(_fetch_json, url)

        # 最初の時系列データから天気を抽出
        time_series = data[0]["timeSeries"][0]
        weather = time_series["areas"][0]["weathers"][0]
        # 気温情報（あれば）
        temp_info = ""
        if len(data[0]["timeSeries"]) > 2:
            temps = data[0]["timeSeries"][2]
            if temps.get("areas"):
                temp_area = temps["areas"][0]
                temps_list = temp_area.get("temps", [])
                time_defines = temps.get("timeDefines", [])
                temp_info = _format_temps(time_defines, temps_list)

        result = {
            "area": area,
            "weather": weather.replace("\u3000", " "),
            "temperature": temp_info or "情報なし",
        }
        if note:
            result["message"] = note
        return result

    except Exception as e:
        return {
            "area": area,
            "weather": "取得失敗",
            "error": str(e),
        }
