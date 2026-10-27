# Amazon Nova 2.5 Sonic を「動かして」分かった、speech-to-speech アプリ実装の勘所 ― Nova 2 からの移行・barge-in・認証の落とし穴まで【AWS Deep Cuts】

> **レベル**: 200〜300（Amazon Bedrock を触ったことがあり、非同期 I/O や WebSocket の基本が分かる方向け）
> **ハンズオン教材**: https://github.com/AWS-Deep-Cuts/amazon-nova-2-5-sonic

## TL;DR

- Amazon Nova 2.5 Sonic（2026 年 10 月 GA）は、音声認識・推論・音声合成を **1 つのモデル** で行う speech-to-speech 基盤モデル。API は HTTP/2 上の **双方向イベントストリーム** (`InvokeModelWithBidirectionalStream`) で、「リクエストを投げて結果を待つ」従来の生成 AI API とは設計の前提がまるで違う。
- 前世代の Nova 2 Sonic とは API・voiceId・対応言語が共通。移行の中心は **モデル ID の変更**（`amazon.nova-2-5-sonic`。`-v1:0` が付かない）と、**より指示に忠実で簡潔になったモデルに合わせたプロンプトの見直し**。モデル ID を書いた IAM ポリシーの更新も忘れやすい。
- ドキュメントを読んだだけでは分からず、実際に作って初めて踏んだ落とし穴がいくつもあった（いずれも Nova 2 Sonic で作り込んだときのもので、API が共通の 2.5 にもそのまま当てはまる）。
  - システムプロンプトに「先に話して」と書いても、AI は先に話さない（ユーザー入力が発火トリガー）
  - barge-in はモデル側で止まっても、**クライアントに溜まった音声が鳴り続ける**
  - 字幕が二重に出る（`SPECULATIVE` と `FINAL`）
  - 発話スピードはプロンプトで変えられない
  - 公式に記載のある「話し方に合わせて応答を調整する（Adaptive speech response）」は、2.5 で試した限りはっきり確認できなかった。ブラウザのマイクの自動音量調整が既定で有効になり、声の大小がそろえられてしまう点も見落としやすい
- 2026 年時点の Python SDK (`aws-sdk-bedrock-runtime` 0.11) は **Python 3.12 以上必須**。既定の設定では **IAM Identity Center (SSO) の認証情報を解決できず**、既定の HTTP トランスポート（aiohttp）は **双方向ストリーミング非対応**。この 2 点は明示的な対処が必要。

---

## はじめに

AWS Deep Cuts は、AWS の中でも情報が少ない「隠れた名曲 = Deep Cuts」を、前提知識からハンズオンまで掘り下げるシリーズです。今回は Amazon Nova 2.5 Sonic を取り上げます。

この記事は公式ドキュメントの要約ではありません。ローカル PC 上に「プロンプトや設定を書き換えながら Nova Sonic と話せるプレイグラウンド」を作り、動かしながら何度も作り直した過程で **気付いたこと・つまずいたこと・設計判断の理由** を中心にまとめています。プレイグラウンドは最初 Nova 2 Sonic で作り込み、2026 年 10 月の Nova 2.5 Sonic の GA に合わせて移行しました。仕様の記述は、2026 年 10 月時点の公式ドキュメントと、AWS 公式サンプルリポジトリ（[aws-samples/amazon-nova-samples](https://github.com/aws-samples/amazon-nova-samples)）の移行ガイドで確認できた範囲に限っています。

---

## 1. Nova 2.5 Sonic とは何か ― 「3 段パイプライン」との違い

従来の音声対話は、次の 3 段パイプラインで作るのが定番でした。

```text
[音声] → Amazon Transcribe (ASR) → [テキスト] → LLM → [テキスト] → Amazon Polly (TTS) → [音声]
```

この構成には、構造上どうしても越えられない壁があります。

1. **韻律 (prosody) が消える**: ASR でテキストにした時点で、声のトーン・速さ・抑揚・ためらいといった情報が失われる。LLM は「ユーザーがどう話したか」を知り得ない。
2. **ターン管理を自前で作る必要がある**: 「ユーザーが話し終わったか」の判定 (endpointing) と、AI の発話中にユーザーが割り込んだときの処理 (barge-in) を、各サービスの外側で実装しなければならない。
3. **レイテンシが直列に積み上がる**: 3 つの処理を順番に待つ。

Nova Sonic はこれを 1 つのモデルにまとめます。音声を入れると、応答の **テキスト（字幕）と音声が同時にストリームで返って** きます。音声のまま受け取るので、原理的には「どう話したか」も応答に反映できる構造です。公式ドキュメントにも「入力音声の prosody（ペースや音色）に基づいて応答の話し方を動的に調整する（Adaptive speech response）」という記載があります。ただし、実際に試したところ、この効果ははっきりとは確認できませんでした（3.7 で詳しく書きます）。

### 1.1 押さえておきたい仕様

| 項目 | 内容 |
| -- | -- |
| モデル ID | `amazon.nova-2-5-sonic`（Nova 2 Sonic は `amazon.nova-2-sonic-v1:0`） |
| API | `InvokeModelWithBidirectionalStream`（HTTP/2 双方向イベントストリーム） |
| 必要な IAM アクション | `bedrock:InvokeModel`（API リファレンスに記載） |
| 音声入力 | LPCM / 16bit / モノラル / base64。サンプリングレートは 8kHz・16kHz・24kHz |
| 音声出力 | LPCM / 16bit / モノラル。8kHz・16kHz・24kHz から選択 |
| 1 接続の上限 | 8 分 |
| 対応リージョン (Bedrock) | us-east-1, us-west-2, ap-northeast-1, eu-north-1 |
| 対応言語 | 英語 (US/UK/IN/AU)、スペイン語、フランス語、イタリア語、ドイツ語、ポルトガル語、ヒンディー語 |

> Bedrock のモデルアクセスは 2025 年 10 月から自動有効化になり、「モデルアクセス」画面での申請は不要になりました。アクセス制御は IAM と SCP で行います。私も最初は旧来の手順で「モデルアクセスを有効化してください」と README に書いていて、後から削除しました。古いブログ記事を参考にする場合は注意してください。

**日本語は 2026 年 10 月時点でもサポート言語に含まれていません**（Nova 2.5 Sonic の対応言語は Nova 2 Sonic と同じ）。Nova 2 Sonic のハンズオンで「日本語で答えて」と指示すると、英語にフォールバックしたり、発音が不安定になったりします。日本語のプロダクトにそのまま使えるわけではない点は、先に知っておくべき制約です。

### 1.2 Nova 2 Sonic から何が変わったか

Nova 2.5 Sonic は 2026 年 10 月 5 日に GA となった、Nova Sonic の「推論強化版」です。AWS 公式サンプルリポジトリの移行ガイド（Getting Started / Migrating to Nova 2.5 Sonic）には、Nova 2 Sonic との比較が次のように載っています。

| 観点 | Nova 2 Sonic 比 |
| -- | -- |
| 指示追従（Audio MultiChallenge） | +43% |
| ツール呼び出し（ComplexFuncBench） | +25% |
| マルチステップのタスク完遂（tau3-bench） | +20% |
| 最初の音声が出るまで（TTFA）P50 | 1.50 秒 → 1.08 秒（-28%） |
| 同 P99 | 2.45 秒 → 1.45 秒（-41%） |
| トークン単価 | 同じ（応答が簡潔になるぶん、1 時間あたりのコストは下がる見込み） |

一方で、**API、voiceId、対応言語は Nova 2 Sonic と共通** です。イベントの形もそのままなので、移行は「モデル ID の変更」と「プロンプトの見直し」が中心になります。ガイドでも、移行で問題が起きる主な原因はモデルの差し替えそのものではなく、**2.5 が指示をより文字どおりに守り、より簡潔に答えるようになったことによるプロンプトとのズレ** だと説明されています。実際に移行して気付いた点は、5 章にまとめました。

---

## 2. プロトコルを理解する ― 「イベントの入れ子」がすべて

Nova Sonic の API を最初に読んだとき、戸惑ったのはイベントの多さでした。ただ、構造は単純な **3 階層の入れ子** です。

```text
sessionStart            … 推論パラメータ、ターン検出の感度
└ promptStart           … 出力音声の設定 (voiceId, サンプリングレート)、ツール定義
   ├ contentStart (TEXT, role=SYSTEM, interactive=false)  … システムプロンプト
   │   textInput
   │ contentEnd
   ├ contentStart (AUDIO, role=USER, interactive=true)    … マイク音声 (開きっぱなし)
   │   audioInput, audioInput, audioInput ...
   │ (ここに TEXT の USER 入力や TOOL 結果を差し込める)
   │ contentEnd
   promptEnd
sessionEnd
```

ポイントは、**ユーザー音声の `contentStart` を開いたまま、マイクのチャンクを流し続ける** ことです。1 回の発話ごとに開け閉めするのではありません。「いつ話し終わったか」はモデル側の VAD（Voice Activity Detection）が判断します。

出力側も同じく `contentStart` → `textOutput` / `audioOutput` / `toolUse` → `contentEnd` の単位で返ってきます。`contentEnd` の `stopReason`（`END_TURN`、`PARTIAL_TURN`、`INTERRUPTED`）がクライアント実装の分岐点になります。

### 2.1 ターン検出の感度 `endpointingSensitivity`

`sessionStart.turnDetectionConfiguration.endpointingSensitivity` で、「どれくらいの無音でユーザーが話し終えたとみなすか」を 3 段階で指定できます。公式ドキュメントでは、HIGH が約 1.5 秒、MEDIUM が約 1.75 秒（推奨）、LOW が約 2.0 秒のポーズとされています。

ハンズオンで「I think... um... maybe...」とためらいながら話すと、HIGH では「um」の後のポーズで AI が話し始めてしまい、LOW では最後まで待ってくれます。**差は 0.5 秒しかないのに、体感はまったく違います。** 語学学習のように「考えながら話す」ユーザーが相手なら LOW、コールセンターのようにテンポが大事な場面なら HIGH、という使い分けが現実的だと感じました。

---

## 3. 実装してみて分かったこと

ここからが本題です。この章の体験は、Nova 2 Sonic でプレイグラウンドを作り込んだときのものです。Nova 2.5 Sonic でもイベントの仕様は同じなので、クライアント側の学びはそのまま通用します。ハンズオン用プレイグラウンドは、以下の構成で作りました。

```text
┌──────────────┐  WebSocket (127.0.0.1)  ┌──────────────┐  HTTP/2 双方向ストリーム  ┌──────────────┐
│  index.html  │ ◀────────────────────▶ │  server.py   │ ◀──────────────────────▶ │Nova 2.5 Sonic│
│  (ブラウザ)   │  PCM 16kHz / 24kHz      │  (中継)       │                          │  (Bedrock)   │
│  マイク/再生  │                         │  ツール実行   │ ── HTTP ──▶ 気象庁 API   │              │
└──────────────┘                         └──────────────┘                          └──────────────┘
```

ブラウザから直接 Bedrock を呼ばず、ローカルの中継サーバーを挟んでいるのは、**AWS の認証情報をブラウザに渡さない** ためです。

### 3.1 最初は「CloudShell + スクリプト」で作って、捨てた

実は最初の版は、このシリーズの他のハンズオン（Entity Resolution、Local Zones）と同じく、CloudShell でスクリプトを実行して結果を確認する形式でした。事前に用意した音声ファイルを送り、返ってきた音声を WAV にして HTML に埋め込み、ダウンロードして聴く、という流れです。

動きはしたのですが、作ってみて **これでは Nova Sonic の価値がまったく伝わらない** と気付きました。録音済みの音声を投げて返事を聴くだけなら、3 段パイプラインと体験が変わりません。割り込みや、ためらい（間）への反応といった、このモデルならではの特性は **リアルタイムで会話して初めて体感できる** ものです。そこで、ローカル PC のマイクで会話するプレイグラウンド形式に作り直しました。

その過程で、Windows の Git Bash や WSL から Windows 側の `aws.exe` を呼ぶと引数が壊れる問題にも当たりました。受講者の OS を選ばないように、セットアップを bash から Python（`setup.py`）に書き直しています。

### 3.2 「先に話して」と書いても、AI は先に話さない

英会話講師のように、**AI の側から挨拶して会話を始めたい** というのは自然な要件です。私は当初、システムプロンプトに次のように書けば実現できると考えていて、初期の原稿にもそう書いていました。

```text
You are a friendly English tutor. As soon as the session begins, greet the student ...
Do not wait for the user to speak first.
```

実際には、**何も話さなければ AI は沈黙したまま** です。Nova Sonic は、ユーザーからの入力（音声、またはテキスト）があって初めて応答を生成します。システムプロンプトは「何を話すか」は決めても、「いつ話し始めるか」は決めません。

AI から話し始めさせるには、公式ドキュメントの **Cross-modal input** を使います。音声ストリームを開いたまま、`role: USER`・`interactive: true` の **テキスト** content を差し込みます。

```python
async def inject_text(self, text: str):
    cn = str(uuid.uuid4())
    await self._send_event({"event": {"contentStart": {
        "promptName": self.prompt_name, "contentName": cn,
        "type": "TEXT", "interactive": True, "role": "USER",
        "textInputConfiguration": {"mediaType": "text/plain"},
    }}})
    await self._send_event({"event": {"textInput": {
        "promptName": self.prompt_name, "contentName": cn, "content": text,
    }}})
    await self._send_event({"event": {"contentEnd": {
        "promptName": self.prompt_name, "contentName": cn,
    }}})
```

送るテキストは `Start` のような短いもので十分です。**挨拶の中身はあくまでシステムプロンプトが決め、テキスト入力は「今話し始めてよい」という合図にすぎない** というのが、動かしてみて腹落ちした理解です。

プレイグラウンドには一度、セッション開始と同時にこのテキストを自動送信する機能を入れました。しかし最終的には外し、受講者が自分でテキストを送る手順に戻しています。自動化すると「AI が勝手に話し始めた」ように見えてしまい、**何が発火トリガーなのか** というこの機能の本質が見えなくなるためです。

### 3.3 barge-in は「モデルが止まっても、音は止まらない」

Nova Sonic は barge-in（AI の発話中の割り込み）にネイティブ対応しています。AI が話している最中にユーザーが話し始めると、モデルは生成を中断し、`contentEnd` に `stopReason: "INTERRUPTED"` を付けて知らせます。会話のコンテキストは保持されます。

ところが、最初の実装では **割り込んでも AI の声が数秒間鳴り続けました。** 原因はモデルではなくクライアントです。Nova Sonic は音声を **実時間より速く** 生成して送ってくるため、ブラウザ側には再生待ちの音声が溜まっています。モデルが生成をやめても、すでに受け取った分は再生されてしまいます。

公式ドキュメントにも、クライアント側で再生を止めてキューを破棄する必要があると書かれています。つまり、**barge-in はモデルとクライアントの共同作業** です。

```python
# server.py: INTERRUPTED を検知したらブラウザに通知
if stop_reason == "INTERRUPTED":
    await self._send_ws({"type": "clearAudio"})
```

```javascript
// index.html: スケジュール済みの AudioBufferSourceNode をすべて止める
function clearAudioQueue() {
  for (const s of scheduledSources) { try { s.stop(); } catch (e) {} }
  scheduledSources = [];
  if (playCtx) nextPlayTime = playCtx.currentTime;
}
```

Web Audio API で「前の再生が終わる時刻 (`nextPlayTime`) に次のチャンクを予約する」方式で再生している場合、予約済みのノードへの参照を持っておかないと止められません。音声の再生キューは、最初から **止められる前提** で設計しておくべきだと学びました。

これを直すと、「長い物語を話させている途中で『Stop! 猫の名前は何だった？』と割り込む」操作で、AI が即座に黙り、中断前の話を踏まえて答えるようになります。ハンズオンの中でも、特に Nova Sonic らしさを体感できる操作です。

### 3.4 字幕が二重に出る ― `SPECULATIVE` と `FINAL`

AI の発話テキストをログに出すと、**同じ文が 2 回表示** されました。

Nova Sonic の ASSISTANT 側のテキストには 2 種類あります。`contentStart` の `additionalModelFields` に入っている `generationStage` で区別します。

| generationStage | 意味 | 届くタイミング |
| -- | -- | -- |
| `SPECULATIVE` | これから話す予定の内容のプレビュー | 音声合成の前 |
| `FINAL` | 実際に発話された内容の文単位の書き起こし | 音声の後 |

`generationStage` は `textOutput` 自体ではなく、**直前の `contentStart` に載って** きます。そのため、`contentName` をキーにステージを覚えておき、`textOutput` を受け取ったときに引く、という状態管理が必要です。

```python
# contentStart でステージを記録
fields = json.loads(cs["additionalModelFields"])
self._content_stages[cs["contentName"]] = fields.get("generationStage", "")

# textOutput では FINAL を捨てる (SPECULATIVE と内容が重複するため)
if role == "ASSISTANT" and self._content_stages.get(content_name) == "FINAL":
    return
```

どちらを採用するかは用途次第です。字幕を **音声より先に** 出したい（聴覚障がいのある方向けのリアルタイム字幕など）なら `SPECULATIVE`、**実際に話した内容** を会話ログとして残したいなら `FINAL` です。barge-in で途中まで話して止まった場合、`SPECULATIVE` には話さなかった部分も含まれ得る点も意識しておく必要があります。

### 3.5 発話スピードは、プロンプトでは変えられない

語学学習用途では「ゆっくり話してほしい」という要件がよくあります。私は当初、システムプロンプトで制御できると考えていて、「ゆっくり話して」と「スポーツ実況のように速く話して」を比べる手順をハンズオンに入れていました。

実際に試すと、**言葉選びは変わっても、話す速さはほとんど変わりません。** 公式のプロンプトガイドにも、音声パラメータは直接制御できず、影響を与えられるのは生成される内容を通じてだけだと書かれています。

> "While you can't control voice parameters directly, you can influence how natural and engaging the spoken interaction feels through the content generated."

「ユーザー自身がゆっくり話せば、AI もそれに合わせるのでは」とも考えました。しかしこれも、3.7 で書く Adaptive speech response の検証と同じく、公式に裏付けのある確実な方法ではありません。

Nova 2.5 Sonic でも API は共通で、話す速さを指定するパラメータはありません。どうしてもゆっくり聴かせたい場合は、**アプリ側で再生の仕方を変える** しかありません。プレイグラウンドでは、ブラウザ側の再生速度を変える設定（Web Audio API の `AudioBufferSourceNode.playbackRate`）でこれを試せるようにしています。ただし **`playbackRate` は再生速度と一緒に音程も変える** ので、0.7 倍程度まで落とすと声が低くこもって聞こえます。音程を保ったまま速度だけを変えるには、Time-stretch（WSOLA など）の処理を自前で入れる必要があります。プロダクトで本格的に対応するなら、この点は工数として見込んでおくべきです。

この再生速度の設定には、もう一つ反省点があります。最初は、スライダーをテキスト入力欄の横（会話の操作と同じ場所）に置いていました。これでは、**入門者には Nova の機能で話す速さを変えているように見えてしまいます**。実際には、受け取った音声をブラウザが速く / 遅く再生しているだけです。そこで設定パネルを「Nova 2.5 Sonic に送る設定」と「このプレイグラウンドの設定（ブラウザ側）」の 2 つに分け、再生速度は後者に移しました。1.0 倍以外のときは、画面上部に「再生速度（ブラウザ側）」と常に表示するようにしています。新しいサービスを紹介するハンズオンでは、**どこまでがサービスの機能で、どこからがアプリの工夫なのか** を画面の上でも区別しておくことが大切だと感じました。

### 3.6 Tool use は「受信ループを止めない」ことが重要

Tool use の流れ自体は Converse API とよく似ています。モデルが `toolUse` イベント（`toolName`, `toolUseId`, `content` = 引数の JSON）を送ってきたら、クライアントは `type: TOOL`・`role: TOOL` の content として `toolResult` を返します。

ただし、ツール定義の書き方は Converse API と同じではありません。ハンズオンでは、Tool use をオンにするとセッションが「リクエストの内容が不正です（`ValidationException`）」で始まらない、という問題に当たりました。原因はツール定義の `inputSchema.json` です。Converse API では JSON スキーマを **オブジェクト** のまま渡しますが、Nova Sonic の `promptStart` では JSON スキーマを **文字列にして** 渡します。AWS 公式サンプルも Strands Agents の実装も、`json.dumps()` してから渡しています。

```python
"toolConfiguration": {
    "tools": [{
        "toolSpec": {
            "name": "get_weather",
            "description": "Get today's weather forecast for a location in Japan. ...",
            # ✕ "json": {"type": "object", ...}    ← Converse API の書き方
            "inputSchema": {"json": json.dumps({"type": "object", ...})},  # ○ 文字列にする
        }
    }]
}
```

Converse API のツール定義を流用すると、ここでつまずきます。あわせて、公式サンプルでは使われていない `toolChoice: {"auto": {}}` も外しました（指定しなければ、ツールを呼ぶかどうかはモデルが判断します）。

音声ならではの注意点は、**ツールの実行中も双方向ストリームは流れ続けている** ことです。Nova Sonic は非同期のツール呼び出しに対応しており、ツールの実行中もユーザーの新しい入力に応答し続けます。

最初の実装では、レスポンスの受信ループの中で気象庁 API を同期的に呼んでいました（`urllib` による同期 I/O）。そのため、API の応答を待つ間は受信ループ全体が止まっていました。ツールの実行は別タスクに切り出し、同期 I/O はスレッドに逃がすのが正解です。

```python
elif "toolUse" in evt:
    # ツール実行は別タスクで行い、レスポンス受信ループを止めない
    task = asyncio.create_task(self._run_tool(tool_name, tool_use_id, tool_content))
    self._tool_tasks.add(task)
    task.add_done_callback(self._tool_tasks.discard)
```

```python
# tools/weather_mcp.py: 同期 I/O はスレッドで実行する
data = await asyncio.to_thread(_fetch_json, url)
```

また、ツールが失敗しても例外を投げっぱなしにせず、**エラー内容を `toolResult` としてモデルに返す** ようにしています。こうすると、モデルは失敗したことを踏まえて、会話の中でユーザーに伝えられます。音声 UI では画面にエラーが出ても気付かれないことが多いので、**失敗も会話の中で伝える** 設計が重要だと感じました。

### 3.7 「話し方に合わせて応答が変わる」は確認できなかった

ハンズオンには当初、「同じ質問を普通のトーン・ハイテンション・疲れた小声で話しかけると、AI の応答のトーンも変わる」ことを確認する観点を入れていました。公式ドキュメントにある Adaptive speech response を体感してもらう狙いです。

ところが Nova 2.5 Sonic で実際に試すと、**3 通りの話し方のどれに対しても、AI は同じ文（"I am doing well and I am happy to be chatting with you."）を、聴いた限り同じ調子で返してきました。** そこで、何か見落としがないかを調べました。

**1. 公式の記載は「1 行」だけだった**

Adaptive speech response は、前世代モデルの Nova Sonic ユーザーガイドに、機能一覧の 1 行として書かれています。しかし、Nova 2.5 Sonic の発表、モデルの移行ガイド、プロンプトガイドには、この機能への言及がありません。有効にするための設定項目も無く、どの程度反映されるかを示す評価も公開されていません。つまり、**「設定を忘れていた」のではなく、そもそも開発者が制御・確認できる機能として定義されていない** ことが分かりました。

**2. ブラウザが声の大小を消していた**

もう一つ見つかったのは、こちら側の問題です。プレイグラウンドはマイクを次の設定で取得していました。

```javascript
navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
```

`autoGainControl`（自動音量調整）を指定していませんが、Chrome では指定しないと **有効** になります（`getSettings()` で確認すると `autoGainControl: true`）。自動音量調整は、大きな声は下げ、小さな声は上げて、音量をそろえる処理です。つまり、「大声のハイテンション」と「ぼそぼそ声」の一番大きな違いである音量は、**Nova に届く前にブラウザがそろえてしまっていました。** ノイズ抑制も、声の細かな特徴を削る方向に働きます。

ビデオ会議のように「聞き取りやすさ」が目的なら、これらの処理は便利です。しかし、話し方そのものをモデルに渡したい speech-to-speech アプリでは、**音声の前処理がモデルの入力を変えてしまう** ことを意識する必要があります。プレイグラウンドには、自動音量調整とノイズ抑制をオフにする設定を「このプレイグラウンドの設定（ブラウザ側）」として追加しました（Nova の設定ではなく、ブラウザがマイク音声に行う処理の設定です）。エコー除去だけは常にオンにしています。オフにすると AI が自分の声を拾い、自分で自分の発話に割り込んでしまうためです。

**3. 同じ会話の中で同じ質問を繰り返していた**

ログを見ると、3 回の質問を同じセッションの中で続けていました。同じ会話の中で同じ質問を繰り返すと、モデルは前の自分の応答に合わせて同じ答えを返しやすくなります。比較するなら、1 回ごとにセッションを開き直すべきでした。

これらを踏まえて、ハンズオンのこの観点は「話し方に合わせて応答が変わることを確認する」ではなく、「**変わるかどうかを検証する**」に改めました。自動補正をオフにし、1 回ごとに会話をリセットして聴き比べる手順にしています。モデルの機能一覧に書いてあることでも、自分のアプリで頼る前には、**入力経路も含めて自分の耳で確かめる** べきだというのが、この件で得た一番の教訓です。

---

## 4. Python SDK と認証の落とし穴（2026 年 10 月時点）

ここは、ハンズオンを「誰の環境でも動く」ようにする過程で一番時間を使った部分です。

### 4.1 boto3 では呼べない。新しい SDK は Python 3.12 以上

`InvokeModelWithBidirectionalStream` は、boto3 からは呼び出せません。Python から使うには、Smithy ベースの新しい SDK `aws-sdk-bedrock-runtime` を使います。この SDK は **Python 3.12 以上が必須** で、バージョン 1.0 未満のため破壊的変更もあり得ます。

私は当初、README に「Python 3.9 以上」と書いていました。手元の環境が新しかったので問題に気付かなかったのです。Python 3.11 の環境で試したところ `pip install` が「No matching distribution found」で失敗し、ようやく原因が分かりました。今は `setup.py` の最初でバージョンを確認し、足りなければ OS 別のインストール方法を案内しています。依存関係は `requirements.txt` で上限を含めて固定しています。

### 4.2 既定のトランスポートでは双方向ストリームが開けない

`aws-sdk-bedrock-runtime` 0.11 系では、既定の HTTP クライアントが aiohttp になっています。これは双方向ストリーミング（HTTP/2 で送受信を同時に行う duplex 通信）に対応していないため、Nova Sonic を呼ぶと次のエラーになります。

```text
UnsupportedTransportError: The configured transport (AIOHTTPClient) does not support
duplex (bidirectional) event streaming ...
```

`aws-sdk-bedrock-runtime[awscrt]` を入れ、**AWS CRT の HTTP クライアントを明示的に指定** します。

```python
from smithy_http.aio.crt import AWSCRTHTTPClient

config = await AsyncBedrockRuntimeConfig.resolve(
    region=region,
    aws_credentials_identity_resolver=create_credentials_resolver(),  # 4.3 で解説
    transport=AWSCRTHTTPClient(),
)
client = AsyncBedrockRuntimeClient(config=config)
```

トランスポートを指定していない古いバージョン前提のサンプルコードは、最新の SDK ではそのままでは動きません。**SDK のバージョンを固定しないと、ある日突然動かなくなる** 典型例だと思います。Nova 2.5 Sonic の公式移行ガイドでも、「Nova Sonic には duplex 対応の HTTP トランスポート（AWS CRT）が必要で、aiohttp ではない」ことが既知の注意点として挙げられています。

### 4.3 IAM Identity Center (SSO) のプロファイルでは認証が通らない

企業で AWS を使う場合、今は IAM Identity Center 経由の一時認証情報が主流です。ところが、この SDK の標準クレデンシャルチェーンは、**そのままでは SSO プロファイルを解決できません。** 既定で有効なのは、環境変数・プロファイルの静的キー・`credential_process` などに限られます。SSO、`aws login`、AssumeRole、コンテナロール（CloudShell や ECS）は、それぞれ別パッケージのプロバイダーを追加しないと使えません。試すと次のようなエラーになります。

```text
IdentityChainError: No credential providers were configured to resolve an identity.
EcsContainer credential source was detected but no provider claims it; install 'aws-credentials-http'.
```

公式のサンプルも、`AWS_ACCESS_KEY_ID` などを環境変数に設定する前提で書かれています。しかし「Identity Center のポータルから一時キーをコピーして環境変数に貼る」運用は、数時間で失効するうえ、失効したキーが環境変数に残っているとプロファイルより優先されてしまい、原因の分かりにくいエラーになりがちです。

そこでハンズオンでは、**boto3（botocore）のクレデンシャル解決を、Smithy SDK の `IdentityResolver` として橋渡し** しました。botocore は SSO、`aws login`（botocore 1.41 以降）、AssumeRole、コンテナロール、IMDS まで対応しており、一時認証情報の自動更新もしてくれます。

```python
from smithy_aws_core.identity import AWSCredentialsIdentity
from smithy_core.aio.interfaces.identity import IdentityResolver

class BotocoreCredentialsResolver(IdentityResolver):
    def __init__(self, session):
        self._session = session
        self._credentials = None

    async def get_identity(self, *, properties) -> AWSCredentialsIdentity:
        if self._credentials is None:
            # SSO トークンキャッシュの読み込みなどブロッキング I/O があるのでスレッドで
            self._credentials = await asyncio.to_thread(self._session.get_credentials)
        frozen = await asyncio.to_thread(self._credentials.get_frozen_credentials)
        return AWSCredentialsIdentity(
            access_key_id=frozen.access_key,
            secret_access_key=frozen.secret_key,
            session_token=frozen.token,
            expiration=getattr(self._credentials, "_expiry_time", None),
        )
```

これで、`aws sso login` 済みのプロファイルを `AWS_PROFILE` で指定するだけで動くようになりました。setup.py は、認証方式（SSO / aws login / AssumeRole / アクセスキー…）と、実際に使われる IAM ロールの ARN を表示します。さらに **実際に双方向ストリームを 1 本開いて**、権限（`bedrock:InvokeModel`）とリージョンを事前に検証します。`sts:GetCallerIdentity` が通っても、許可セットに Bedrock の権限がなければ会話は始まらないからです。

### 4.4 エラーは「どこで」出るかを知っておく

双方向ストリームでは、エラーが出る場所が HTTP の 1 往復とは違います。実装して分かったのは次の 3 つです。

1. **認証・権限・リージョンのエラー**（`AccessDeniedException`、`UnrecognizedClientException` など）は、ストリームを開く呼び出しではなく、**応答ヘッダーを待つ `await_output()` で** 例外になります。
2. **セッション中のエラー**（`ValidationException`、`ThrottlingException`、`ModelTimeoutException` など）は、例外として飛んでくるのではなく、**出力ストリームのユニオン型のメンバーとして届きます。** `receive()` の戻り値の型を見て判定しないと、「`ValidationException` object has no attribute 'bytes_'」のような意味不明なエラーに化けます。
3. ストリームが正常に閉じたときは、`receive()` が `None` を返します。

```python
_, output_stream = await self.stream.await_output()   # ← 認証・権限エラーはここ
while True:
    result = await output_stream.receive()
    if result is None:                                  # ← 正常終了
        break
    if not isinstance(result, InvokeModelWithBidirectionalStreamOutputChunk):
        raise result.value                              # ← ValidationException など
    await self._handle_output(json.loads(result.value.bytes_.decode("utf-8")))
```

ハンズオンでは、これらの例外を「何が起きたか」「どうすればよいか」の形に変換して、ブラウザとターミナルの両方に表示しています。たとえば SSO セッションが切れていれば `aws sso login --profile xxx` を、権限不足なら必要なアクションと対象 ARN、Identity Center の許可セットの確認を案内します。**音声アプリは「何も起きない」ことが最悪の失敗体験** です。マイクに話しかけても無反応、では受講者は何を疑えばよいか分かりません。公式の移行ガイドにも、フレームワークによっては認証情報の誤りが例外にならず、`receive()` でイベントを待ったまま無反応になることがある、という注意書きがあります。エラーを確実に表面化させる作りは、どの実装でも意識しておく価値があります。

### 4.5 localhost の WebSocket は、それだけでは安全ではない

中継サーバーは `127.0.0.1` にのみバインドしています。外部ネットワークからは接続できません。しかし **ブラウザで開いている任意の Web サイトの JavaScript からは、`ws://127.0.0.1:8765` に接続できてしまいます**（Cross-Site WebSocket Hijacking）。WebSocket には CORS のようなブラウザ側の制限がないためです。この中継サーバーは手元の AWS 認証情報で Bedrock を呼ぶので、悪意のあるページに開かれると他人に Bedrock を使われかねません。

対策として、WebSocket ハンドシェイクの `Origin` ヘッダーを検証し、`file://` で開いた `index.html`（Origin は `null`）と localhost 以外を拒否しています。

```python
ALLOWED_ORIGINS = [None, "null", re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")]
server = await serve(handle_connection, "127.0.0.1", 8765, origins=ALLOWED_ORIGINS)
```

「localhost だから安全」は、ローカル開発ツール全般で見落とされがちな点です。

---

## 5. Nova 2 Sonic から 2.5 Sonic へ移行してみて

ハンズオンも Nova 2.5 Sonic に移行しました。API が共通なのでコードの変更はわずかでしたが、それでも気を付けるべき点がいくつかありました。

### 5.1 モデル ID の形式が変わった

| モデル | モデル ID |
| -- | -- |
| Nova 2 Sonic | `amazon.nova-2-sonic-v1:0` |
| Nova 2.5 Sonic | `amazon.nova-2-5-sonic` |

2.5 には **`-v1:0` のようなバージョン接尾辞が付きません**。旧 ID の感覚で `amazon.nova-2-5-sonic-v1:0` と書くと、存在しない ID になります。ハンズオンではモデル ID を 1 か所（`aws_auth.py`）にまとめていたので、変更は 1 行で済みました。さらに環境変数 `NOVA_SONIC_MODEL_ID` で上書きできるようにして、**同じプロンプトのまま 2 と 2.5 を聴き比べられる** ようにしています。公式ガイドには、アカウントやリージョンによって直接呼び出せない場合はクロスリージョン推論プロファイル（`us.amazon.nova-2-5-sonic`）を試すように、とも書かれています。

### 5.2 IAM ポリシーも「モデル ID 込み」で更新が必要

最小権限の IAM ポリシーでは、`Resource` に基盤モデルの ARN（`arn:aws:bedrock:*::foundation-model/amazon.nova-2-sonic-v1:0`）を書いているはずです。**コードだけ 2.5 に変えると、このポリシーに一致せず `AccessDeniedException` になります。** IAM Identity Center の許可セットでモデルを絞っている組織では、アプリの変更と許可セットの更新（と再プロビジョニング）をセットで計画する必要があります。クロスリージョン推論プロファイルを使う場合は、プロファイルの ARN と転送先の基盤モデルの ARN の両方に権限が必要です。ハンズオンのエラー表示も、使っているモデル ID に合わせて対象 ARN を案内するようにしました。

### 5.3 プロンプトは「否定形」を避けて書き直す

2.5 で一番大きいのはプロンプトの扱いです。AWS 公式の Nova Sonic 2.5 プロンプトガイドでは、次のような考え方が最初に示されています。

- **コンテキストにあるものは、何でも発話されうる。** 「〜とは言わないで」と書くと、その言葉自体が文脈に入り、かえって発話されやすくなる。望む振る舞いを肯定形で書く。
- 「never」「always」「do not」のような強い表現は、2.5 では **より文字どおりに** 守られる。2 の冗長さを抑えるために入れた強い指示が、2.5 では効きすぎる。
- 箇条書きで 1 行 1 ルールにし、指示は 1 回だけ明確に書く。

ハンズオンには、`setup.py` の完了時に AI から「Welcome to AWS Deep Cuts!」と一言あいさつさせる処理（Cross-modal input による Model-start-first の応用）があります。このシステムプロンプトも、ガイドに沿って書き直しました。

```text
# Before（Nova 2 Sonic 向けに書いたもの）
... say exactly the following sentence ..., and nothing else: "Welcome to AWS Deep Cuts! ..."
Do not ask any questions and do not add anything before or after it.

# After（Nova 2.5 Sonic 向け）
You are the cheerful voice of AWS Deep Cuts, a tech series that digs deep into hidden gems of AWS.
- When the session starts, greet the listener with exactly this one sentence, in an energetic tone: "Welcome to AWS Deep Cuts! ..."
- That single sentence is your whole reply; end your turn right after it.
```

「質問しないで」「前後に何も足さないで」と禁止事項を並べるのをやめ、「この 1 文が返答のすべてで、言い終えたらターンを終える」と **望む状態** を書いています。音声モデルのプロンプトは、テキストのチャットボット以上に「書いたことが口に出る」ものとして扱うべきだ、というのが移行作業での一番の気付きでした。

なお、応答の簡潔さやレイテンシの改善が実際の会話でどう聞こえるかは、使い方によって大きく変わります。ハンズオンでは `NOVA_SONIC_MODEL_ID` を切り替えるだけで両モデルを試せるので、ぜひ自分の耳で聴き比べてみてください。

---

## 6. 8 分の壁と、セッション設計

1 本のストリームは **8 分で切断** されます。この上限は Nova 2.5 Sonic でも変わっていません。短い問い合わせ対応なら問題ありませんが、英会話レッスンのように長く話す用途では必ずぶつかります。

プレイグラウンドでは、経過時間を表示して 7 分 30 秒で警告を出し、切断後は Start で再開する、という素朴な作りにしています。プロダクトで途切れなく続けるなら、次のような設計が必要になります。

- 上限の少し前に **新しいストリームを裏で開き**、それまでの会話履歴を `interactive: false` のテキスト content（USER / ASSISTANT のロール付き）として渡して文脈を引き継ぐ
- AI の発話中に切り替えないよう、`END_TURN` の直後など **ターンの切れ目を狙って** 切り替える

Nova Sonic はコンテキストウィンドウ自体は大きいので、履歴を渡すこと自体は難しくありません。むしろ **いつ・どう切り替えればユーザーに気付かれないか** という UX 設計が本質的な課題です。

---

## 7. ハンズオンで体験できること

リポジトリのプレイグラウンドでは、プロンプトと設定 JSON を書き換えながら、以下の 8 つの観点を確認できます。

1. 通常のプロンプトでは AI が自分から話しかけてこないこと
2. Cross-modal input（テキスト送信）で AI から先に話しかけさせる（Model-start-first）
3. `voiceId` による話者の変更
4. ポリグロットボイス（`matthew` / `tiffany`）での言語切り替えと、日本語が非対応であること
5. `endpointingSensitivity` によるターンテイキングの違いと barge-in
6. 声の抑揚・テンションが応答に影響するかの検証（公式の記載と実際の挙動の比較）
7. Tool use による外部 API（気象庁）の呼び出し
8. 発話スピードをプロンプトでは制御できないこと

設定 UI は、最初は voiceId や感度をドロップダウンで選ぶ形にしていました。しかし最終的には、`sessionStart` / `promptStart` に渡す **JSON をそのまま編集する形** にしています。ハンズオンで触った JSON が、そのまま自分のアプリで書くリクエストになるようにするためです。

```bash
git clone https://github.com/AWS-Deep-Cuts/amazon-nova-2-5-sonic.git
cd amazon-nova-2-5-sonic/hands-on
python setup.py   # Python 3.12 以上。認証・権限・リージョンを確認し、AI のあいさつの後にサーバーを起動
# → index.html をブラウザ (Chrome / Edge) で開く
```

Nova 2.5 Sonic は Bedrock のオンデマンド API なので、作成する AWS リソースはありません。料金は入力・出力の音声トークン、テキストトークンの量に応じた従量課金です。最新の単価は [Amazon Bedrock の料金ページ](https://aws.amazon.com/bedrock/pricing/) で確認してください。

---

## 8. まとめ ― 実際に作ってみて

最後に、この記事で扱った処理を「Nova 2.5 Sonic（モデル）がやること」と「アプリがやること」に分けて整理します。speech-to-speech モデルは多くのことを 1 つのモデルで引き受けてくれますが、アプリ側の責務も残ります。

| 場面 | Nova 2.5 Sonic がやること | アプリ（今回のプレイグラウンド）がやること |
| -- | -- | -- |
| 会話の開始 | ユーザーの入力を受けて応答を生成する | AI から話し始めさせたいときは、テキストで合図を送る（3.2） |
| 話し終わりの判定・割り込み | 話し終わりを検出し、割り込まれたら生成を止めて `INTERRUPTED` を通知する | 受信済みの音声の再生を止める（3.3） |
| 字幕 | `SPECULATIVE` と `FINAL` の 2 種類のテキストを送る | どちらを表示するかを選ぶ（3.4） |
| 話す速さ | 速さを指定する設定は無い | 必要ならブラウザ側で再生速度を変える。声の高さも変わる（3.5） |
| ツール | 呼ぶタイミングと引数を決める | ツールを実行し、結果を返す（3.6） |
| マイク音声 | 受け取った音声を理解する | 自動音量調整などの前処理を選ぶ。前処理はモデルへの入力を変えてしまう（3.7） |
| 認証・エラー | — | 認証情報を解決し、失敗したら原因と対処を表示する（4 章） |


- Nova Sonic の本質は「音声認識と合成が速くなった」ことではなく、**音声を直接受け取り、ターン管理（話し終わりの判定と割り込み）がモデルに内蔵された** ことにある。これはリアルタイムで会話して初めて実感できる。
- ただし「モデルに内蔵」されていても、**クライアントの責務は減らない。** barge-in 時の再生キューの破棄、`SPECULATIVE` / `FINAL` の使い分け、ツール実行中も止めない受信ループ、8 分上限の切り替えは、どれもアプリ側の設計で品質が決まる。
- ドキュメントから「できそう」に見えたことの中に、**実際にはできないこと**（システムプロンプトだけで先に話させる、プロンプトで速度を変える、日本語で話す）や、**公式に書いてあっても確認できないこと**（話し方に合わせた応答の調整）があった。PoC の最初の 1 時間で必ず確かめておくべきポイント。
- Python SDK はまだ 1.0 未満で、**Python 3.12 必須・CRT トランスポート指定・SSO 非対応の既定チェーン** という 3 つの落とし穴がある。社内の Identity Center 環境で PoC をする場合は、最初にここを片付けておくと時間を無駄にしない。
- Nova 2 → 2.5 の移行は、コード上はモデル ID の変更でほぼ済む。ただし **IAM ポリシーのモデル ARN** と、**指示に忠実になったモデルに合わせたプロンプト（否定形を避ける）** の 2 点は、事前に計画しておくべき。

音声 UI は、テキストのチャットボットよりも「待たされる」「無視される」ことへの許容度がずっと低いインターフェースです。Nova 2.5 Sonic はその土台をモデル側でさらに引き上げてくれますが、最後の体験を決めるのはやはりアプリケーションの作り込みだ、というのが一番の学びでした。

---

## 参考リンク

- [Amazon Nova 2 Sonic ユーザーガイド](https://docs.aws.amazon.com/nova/latest/nova2-userguide/using-conversational-speech.html)
- [Input events](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-input-events.html) / [Output events](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-output-events.html)
- [Cross-modal input](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-cross-modal.html)
- [Barge-in](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-barge-in.html) / [Turn-taking controllability](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-turn-taking.html)
- [Tool configuration](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-tool-configuration.html)
- [Language support & voices](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-language-support.html)
- [Voice conversation prompts](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-system-prompts.html)
- [InvokeModelWithBidirectionalStream API リファレンス](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_InvokeModelWithBidirectionalStream.html)
- [Amazon Bedrock のサーバーレスモデル自動有効化 (What's New, 2025/10)](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-automatic-enablement-serverless-foundation-models/)
- [Introducing Amazon Nova 2 Sonic (AWS News Blog)](https://aws.amazon.com/blogs/aws/introducing-amazon-nova-2-sonic-next-generation-speech-to-speech-model-for-conversational-ai/)
- [aws-samples/amazon-nova-samples](https://github.com/aws-samples/amazon-nova-samples)
- [Getting Started / Migrating to Nova 2.5 Sonic（aws-samples）](https://github.com/aws-samples/amazon-nova-samples/blob/main/speech-to-speech/amazon-nova-2.5-sonic/Getting_Started_and_Migrating_to_Nova_2.5_Sonic.md)
- [Nova Sonic 2.5 Prompt Engineering — Best Practices Guide（aws-samples）](https://github.com/aws-samples/amazon-nova-samples/blob/main/speech-to-speech/amazon-nova-2.5-sonic/Nova_Sonic_2.5_Prompting_Guide.md)
- [Nova 2.5 Sonic モデルカード（Amazon Bedrock）](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-5-sonic.html)
