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
                            "Area name in Japan, in Japanese or English "
                            "(e.g. '東京' or 'Tokyo', '大阪' or 'Osaka'). "
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


# 英語 (ローマ字) の地域名 → AREA_CODES のキー
# Nova 2.5 Sonic は日本語が非対応のため、英語で会話するとツールの引数も英語になりやすい
ROMAJI_AREAS = {
    "hokkaido": "北海道", "sapporo": "札幌", "aomori": "青森", "iwate": "岩手",
    "miyagi": "宮城", "sendai": "仙台", "akita": "秋田", "yamagata": "山形",
    "fukushima": "福島", "ibaraki": "茨城", "tochigi": "栃木", "gunma": "群馬",
    "saitama": "埼玉", "chiba": "千葉", "tokyo": "東京", "kanagawa": "神奈川",
    "yokohama": "横浜", "niigata": "新潟", "toyama": "富山", "ishikawa": "石川",
    "kanazawa": "石川", "fukui": "福井", "yamanashi": "山梨", "nagano": "長野",
    "gifu": "岐阜", "shizuoka": "静岡", "aichi": "愛知", "nagoya": "名古屋",
    "mie": "三重", "shiga": "滋賀", "kyoto": "京都", "osaka": "大阪",
    "hyogo": "兵庫", "kobe": "神戸", "nara": "奈良", "wakayama": "和歌山",
    "tottori": "鳥取", "shimane": "島根", "okayama": "岡山", "hiroshima": "広島",
    "yamaguchi": "山口", "tokushima": "徳島", "kagawa": "香川", "ehime": "愛媛",
    "kochi": "高知", "fukuoka": "福岡", "saga": "佐賀", "nagasaki": "長崎",
    "kumamoto": "熊本", "oita": "大分", "miyazaki": "宮崎", "kagoshima": "鹿児島",
    "okinawa": "沖縄", "naha": "那覇",
}


def _resolve_area(area: str):
    """地域名 (日本語 / 英語) から (表示名, 地域コード) を返す。見つからなければ None"""
    if area in AREA_CODES:
        return area, AREA_CODES[area]
    # 英語表記: "Tokyo", "Osaka-fu", "Fukuoka Prefecture" などを許容
    letters = "".join(ch for ch in area.lower() if ch.isascii() and ch.isalpha())
    if letters:
        for romaji in sorted(ROMAJI_AREAS, key=len, reverse=True):
            if letters.startswith(romaji):
                key = ROMAJI_AREAS[romaji]
                return key, AREA_CODES[key]
    # 日本語の部分一致 ("東京都", "大阪市" など)
    for key, code in AREA_CODES.items():
        if key in area:
            return key, code
    return None


async def get_weather(params: dict) -> dict:
    """気象庁 API から天気予報を取得"""
    area = str(params.get("area") or "東京").strip() or "東京"

    # 地域コード解決
    resolved = _resolve_area(area)
    area_code = None
    if resolved:
        area, area_code = resolved

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
