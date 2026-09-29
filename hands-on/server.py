"""
AWS Deep Cuts: Amazon Nova 2 Sonic — WebSocket 中継サーバー

ブラウザ (index.html) と Bedrock Nova 2 Sonic の間を中継する。
セキュリティ: 127.0.0.1 にのみバインドし、外部からのアクセスを遮断する。

起動方法:
  python server.py

依存パッケージ (Python 3.12 以上):
  pip install -r requirements.txt
"""

import asyncio
import json
import uuid
import base64
import os
import re
import signal
import sys
import traceback
from typing import Optional

from websockets.asyncio.server import serve

import aws_auth

# ─── 設定 ────────────────────────────────────────────────────────
HOST = "127.0.0.1"  # localhost のみ — セキュリティのため外部バインドしない
PORT = int(os.environ.get("PORT", "8765"))
# 接続を許可する Origin: file:// で開いた index.html ("null") と localhost のみ。
# 127.0.0.1 バインドだけでは、ブラウザで開いた任意の Web サイトから
# ws://127.0.0.1 へ接続される (Cross-Site WebSocket Hijacking) のを防げないため。
ALLOWED_ORIGINS = [
    None,
    "null",
    re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"),
]
REGION = aws_auth.resolve_region()
MODEL_ID = aws_auth.MODEL_ID

# ─── ツール読み込み ──────────────────────────────────────────────
from tools.weather_mcp import get_weather, WEATHER_TOOL_SPEC


# ─── Bedrock クライアント初期化 ──────────────────────────────────
async def _create_bedrock_client():
    """AsyncBedrockRuntimeClient を生成 (認証・通信方式の詳細は aws_auth.py を参照)

    SDK 標準のクレデンシャルチェーンは IAM Identity Center (SSO) や aws login、
    AssumeRole プロファイルに対応していないため、boto3 の解決結果を渡す。
    """
    return await aws_auth.create_bedrock_runtime_client()


def _error_message(exc: BaseException, phase: str) -> dict:
    """例外をブラウザ向けのエラーメッセージに変換し、ターミナルにも出力する"""
    info = aws_auth.describe_error(exc)
    print(f"[Error] {phase}")
    print(aws_auth.format_error_for_console(info))
    if os.environ.get("DEBUG"):
        traceback.print_exception(exc)
    return {"type": "error", "phase": phase, **info}


# ─── Bedrock セッション ──────────────────────────────────────────
class SonicSession:
    """1つのブラウザ接続に対応する Nova 2 Sonic セッション"""

    def __init__(self, ws, config: dict):
        self.ws = ws
        self.config = config
        self.prompt_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self.is_active = False
        self.stream = None
        # contentName → generationStage のマッピング (重複テキスト抑制用)
        self._content_stages: dict[str, str] = {}
        self._content_roles: dict[str, str] = {}
        self._response_task: Optional[asyncio.Task] = None
        self._tool_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._error_sent = False

    async def start(self):
        # クライアントから送られた設定を使用
        session_config = self.config.get("sessionConfig") or {}
        prompt_config = self.config.get("promptConfig") or {}
        system_prompt = (self.config.get("systemPrompt") or "").strip() or "You are a helpful assistant."
        enable_tools = self.config.get("enableTools", False)
        if not isinstance(session_config, dict) or not isinstance(prompt_config, dict):
            raise ValueError("Session Config / Prompt Config は JSON オブジェクト ({...}) で指定してください。")

        self.is_active = True

        # Bedrock クライアント生成 & ストリーム開始
        from aws_sdk_bedrock_runtime.client import (
            InvokeModelWithBidirectionalStreamOperationInput,
        )

        client = await _create_bedrock_client()
        self.stream = await client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL_ID)
        )

        # 1. SessionStart — クライアント指定の設定をそのまま使用
        session_start_event = {
            "event": {
                "sessionStart": session_config
            }
        }
        # デフォルト値の保証
        if "inferenceConfiguration" not in session_config:
            session_start_event["event"]["sessionStart"]["inferenceConfiguration"] = {
                "maxTokens": 1024, "topP": 0.9, "temperature": 0.7
            }
        if "turnDetectionConfiguration" not in session_config:
            session_start_event["event"]["sessionStart"]["turnDetectionConfiguration"] = {
                "endpointingSensitivity": "MEDIUM"
            }
        await self._send_event(session_start_event)

        # 2. PromptStart — クライアント指定の audioOutputConfiguration を使用
        audio_output = prompt_config.get("audioOutputConfiguration", {
            "mediaType": "audio/lpcm",
            "sampleRateHertz": 24000,
            "sampleSizeBits": 16,
            "channelCount": 1,
            "voiceId": "matthew",
            "encoding": "base64",
            "audioType": "SPEECH",
        })
        text_output = prompt_config.get("textOutputConfiguration", {"mediaType": "text/plain"})

        prompt_start: dict = {
            "event": {
                "promptStart": {
                    "promptName": self.prompt_name,
                    "textOutputConfiguration": text_output,
                    "audioOutputConfiguration": audio_output,
                }
            }
        }

        if enable_tools:
            prompt_start["event"]["promptStart"]["toolUseOutputConfiguration"] = {
                "mediaType": "application/json"
            }
            prompt_start["event"]["promptStart"]["toolConfiguration"] = {
                "tools": [WEATHER_TOOL_SPEC],
                "toolChoice": {"auto": {}},
            }

        await self._send_event(prompt_start)

        # 3. System Prompt
        sys_cn = str(uuid.uuid4())
        await self._send_event({
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": sys_cn,
                    "type": "TEXT",
                    "interactive": False,
                    "role": "SYSTEM",
                    "textInputConfiguration": {"mediaType": "text/plain"},
                }
            }
        })
        await self._send_event({
            "event": {
                "textInput": {
                    "promptName": self.prompt_name,
                    "contentName": sys_cn,
                    "content": system_prompt,
                }
            }
        })
        await self._send_event({
            "event": {"contentEnd": {"promptName": self.prompt_name, "contentName": sys_cn}}
        })

        # 4. Audio Input ストリーム開始
        await self._send_event({
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": self.audio_content_name,
                    "type": "AUDIO",
                    "interactive": True,
                    "role": "USER",
                    "audioInputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 16000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "audioType": "SPEECH",
                        "encoding": "base64",
                    },
                }
            }
        })

        # 認証・権限エラーは応答ヘッダー受信時 (await_output) に判明することが多い。
        # 少しだけ待ち、失敗ならブラウザに "Started" を出す前にエラーとして返す。
        try:
            await asyncio.wait_for(asyncio.shield(self.stream.await_output()), timeout=2)
        except asyncio.TimeoutError:
            pass  # 応答ヘッダーが遅いだけなら、そのまま開始する

        # レスポンス処理タスク開始
        self._response_task = asyncio.create_task(self._process_responses())

    async def send_audio(self, base64_data: str):
        """ブラウザから受信した音声チャンクを Nova Sonic に転送"""
        if not self.is_active:
            return
        await self._send_event_safe({
            "event": {
                "audioInput": {
                    "promptName": self.prompt_name,
                    "contentName": self.audio_content_name,
                    "content": base64_data,
                }
            }
        })

    async def inject_text(self, text: str):
        """Cross-modal text input (USER role)"""
        if not self.is_active:
            return
        cn = str(uuid.uuid4())
        await self._send_event_safe({
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": cn,
                    "type": "TEXT",
                    "interactive": True,
                    "role": "USER",
                    "textInputConfiguration": {"mediaType": "text/plain"},
                }
            }
        })
        await self._send_event_safe({
            "event": {
                "textInput": {
                    "promptName": self.prompt_name,
                    "contentName": cn,
                    "content": text,
                }
            }
        })
        await self._send_event_safe({
            "event": {"contentEnd": {"promptName": self.prompt_name, "contentName": cn}}
        })

    async def close(self):
        if self._closing:
            return
        self._closing = True
        was_active = self.is_active
        self.is_active = False
        for task in list(self._tool_tasks):
            task.cancel()
        if self.stream is None:
            return
        # 正常終了のイベントを送る (ストリームが既に切れている場合に備えてタイムアウト付き)
        async def _graceful_end():
            await self._send_event({
                "event": {"contentEnd": {"promptName": self.prompt_name, "contentName": self.audio_content_name}}
            })
            await self._send_event({"event": {"promptEnd": {"promptName": self.prompt_name}}})
            await self._send_event({"event": {"sessionEnd": {}}})

        if was_active:
            try:
                await asyncio.wait_for(_graceful_end(), timeout=3)
            except (Exception, asyncio.TimeoutError):
                pass
        try:
            await asyncio.wait_for(self.stream.input_stream.close(), timeout=3)
        except (Exception, asyncio.TimeoutError):
            pass
        if self._response_task and not self._response_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._response_task), timeout=3)
            except (Exception, asyncio.TimeoutError):
                self._response_task.cancel()

    async def _send_event(self, event: dict):
        """イベントを Bedrock の双方向ストリームに送信"""
        from aws_sdk_bedrock_runtime.models import (
            InvokeModelWithBidirectionalStreamInputChunk,
            BidirectionalInputPayloadPart,
        )

        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(
                bytes_=json.dumps(event).encode("utf-8")
            )
        )
        await self.stream.input_stream.send(chunk)

    async def _send_event_safe(self, event: dict):
        """ストリーム切断後の送信で例外を連発させないためのラッパー"""
        if not self.is_active:
            return
        try:
            await self._send_event(event)
        except Exception as e:
            if self.is_active:
                self.is_active = False
                self._error_sent = True
                await self._send_ws(_error_message(e, "Bedrock への送信"))

    async def _process_responses(self):
        """Bedrock からのレスポンスストリームを処理"""
        from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamOutputChunk

        reason = "closed"
        try:
            # 認証・権限・リージョンのエラーは多くの場合ここで例外になる
            _, output_stream = await self.stream.await_output()
            while True:
                result = await output_stream.receive()
                if result is None:  # サーバー側がストリームを閉じた
                    break
                if not isinstance(result, InvokeModelWithBidirectionalStreamOutputChunk):
                    # ValidationException / ThrottlingException などはユニオンのメンバーとして届く
                    err = result.value if isinstance(getattr(result, "value", None), BaseException) \
                        else RuntimeError(f"{type(result).__name__}: {getattr(result, 'value', '')}")
                    raise err
                if result.value and result.value.bytes_:
                    json_data = json.loads(result.value.bytes_.decode("utf-8"))
                    await self._handle_output(json_data)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._closing:
                reason = "error"
                if not self._error_sent:  # 送信側で既に通知済みなら重複させない
                    self._error_sent = True
                    await self._send_ws(_error_message(e, "Nova 2 Sonic セッション"))
        finally:
            ended_by_user = self._closing
            self.is_active = False
            if not ended_by_user:
                await self._send_ws({"type": "state", "state": "ended", "reason": reason})

    async def _handle_output(self, data: dict):
        """Bedrock からの出力イベントを処理"""
        if "event" not in data:
            return

        evt = data["event"]

        # contentStart — generationStage を追跡
        if "contentStart" in evt:
            cs = evt["contentStart"]
            content_name = cs.get("contentName", "")
            role = cs.get("role", "")
            self._content_roles[content_name] = role
            # additionalModelFields に generationStage が含まれる
            additional = cs.get("additionalModelFields")
            if additional:
                try:
                    fields = json.loads(additional) if isinstance(additional, str) else additional
                    stage = fields.get("generationStage", "")
                    self._content_stages[content_name] = stage
                except (json.JSONDecodeError, TypeError):
                    pass
            return

        # contentEnd — 追跡情報をクリーンアップ + barge-in 検知
        if "contentEnd" in evt:
            content_name = evt["contentEnd"].get("contentName", "")
            stop_reason = evt["contentEnd"].get("stopReason", "")
            self._content_stages.pop(content_name, None)
            self._content_roles.pop(content_name, None)
            # barge-in: stopReason が INTERRUPTED なら音声キューをクリアさせる
            if stop_reason == "INTERRUPTED":
                await self._send_ws({"type": "clearAudio"})
            return

        # テキスト出力 — SPECULATIVE のみ表示し、確定版(FINAL)は無視して重複を防ぐ
        if "textOutput" in evt:
            content = evt["textOutput"].get("content", "")
            role = evt["textOutput"].get("role", "ASSISTANT")
            content_name = evt["textOutput"].get("contentName", "")

            # ASSISTANT ロールで generationStage が記録されている場合
            if role == "ASSISTANT" and content_name in self._content_stages:
                stage = self._content_stages[content_name]
                # FINAL は SPECULATIVE と同じ内容なので無視
                if stage == "FINAL":
                    return

            if content.strip():
                await self._send_ws({"type": "transcript", "role": role, "content": content})

        # 音声出力
        elif "audioOutput" in evt:
            audio = evt["audioOutput"].get("content", "")
            if audio:
                await self._send_ws({"type": "audio", "data": audio})

        # Tool use
        elif "toolUse" in evt:
            tool_name = evt["toolUse"].get("toolName", "")
            tool_use_id = evt["toolUse"].get("toolUseId", "")
            tool_content = evt["toolUse"].get("content", "{}")

            await self._send_ws({
                "type": "toolCall",
                "toolName": tool_name,
                "params": tool_content,
            })

            # ツール実行は別タスクで行い、レスポンス受信ループを止めない
            task = asyncio.create_task(self._run_tool(tool_name, tool_use_id, tool_content))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)

    async def _run_tool(self, tool_name: str, tool_use_id: str, tool_content):
        """ツールを実行し、結果を toolResult としてモデルに返す"""
        result = await self._execute_tool(tool_name, tool_content)
        if "error" in result:
            await self._send_ws({"type": "toolError", "toolName": tool_name, "message": str(result["error"])})

        # ToolResult を返す
        tool_cn = str(uuid.uuid4())
        await self._send_event_safe({
            "event": {
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": tool_cn,
                    "interactive": False,
                    "type": "TOOL",
                    "role": "TOOL",
                    "toolResultInputConfiguration": {
                        "toolUseId": tool_use_id,
                        "type": "TEXT",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    },
                }
            }
        })
        await self._send_event_safe({
            "event": {
                "toolResult": {
                    "promptName": self.prompt_name,
                    "contentName": tool_cn,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            }
        })
        await self._send_event_safe({
            "event": {"contentEnd": {"promptName": self.prompt_name, "contentName": tool_cn}}
        })

    async def _execute_tool(self, tool_name: str, params_json: str) -> dict:
        """ツールを実行して結果を返す"""
        try:
            params = json.loads(params_json) if isinstance(params_json, str) else params_json
        except json.JSONDecodeError:
            return {"error": "Invalid JSON parameters"}

        try:
            if tool_name == "get_weather":
                return await get_weather(params)
            return {"error": f"Unknown tool: {tool_name}"}
        except Exception as e:  # ツールの失敗で会話全体を落とさない
            return {"error": f"{type(e).__name__}: {e}"}

    async def _send_ws(self, msg: dict):
        try:
            await self.ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass


# ─── WebSocket ハンドラー ─────────────────────────────────────────
async def handle_connection(ws):
    print(f"[WS] Client connected")
    session: Optional[SonicSession] = None

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type", "")

            if msg_type == "start":
                if session:
                    await session.close()
                session = SonicSession(ws, msg.get("config", {}))
                try:
                    await session.start()
                except Exception as e:
                    # 認証エラーなどでセッションを開始できなかった場合もブラウザへ通知する
                    await ws.send(json.dumps(_error_message(e, "セッション開始"), ensure_ascii=False))
                    await session.close()
                    session = None
                    await ws.send(json.dumps({"type": "state", "state": "ended", "reason": "error"}))
                    continue
                await ws.send(json.dumps({"type": "state", "state": "active"}))

            elif msg_type == "audio" and session:
                await session.send_audio(msg.get("data", ""))

            elif msg_type == "textInject" and session:
                await session.inject_text(msg.get("content", ""))

            elif msg_type == "stop":
                if session:
                    await session.close()
                    session = None

    except Exception as e:
        # ブラウザ側の切断 (ConnectionClosed) 以外は内容をターミナルに残す
        if type(e).__name__ not in ("ConnectionClosedOK", "ConnectionClosedError", "ConnectionClosed"):
            _error_message(e, "WebSocket 処理")
    finally:
        if session:
            await session.close()
        print(f"[WS] Client disconnected")


# ─── サーバー起動 ─────────────────────────────────────────────────
def _check_credentials():
    """起動時に認証情報を解決できるか確認する (失敗しても起動は継続し、原因を表示)"""
    print(f"  Profile:   {aws_auth.profile_label()}")
    if REGION not in aws_auth.SUPPORTED_REGIONS:
        print(f"  ⚠️  {REGION} は Nova 2 Sonic の対応リージョンではない可能性があります "
              f"(対応: {', '.join(aws_auth.SUPPORTED_REGIONS)})")
    try:
        creds = aws_auth.create_boto3_session().get_credentials()
        if creds is None:
            from botocore.exceptions import NoCredentialsError
            raise NoCredentialsError()
        creds.get_frozen_credentials()
        print(f"  Auth:      OK ({getattr(creds, 'method', 'unknown')})")
    except Exception as e:
        print("  Auth:      NG")
        print(aws_auth.format_error_for_console(aws_auth.describe_error(e)))
    print("")


async def main():
    print("")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  AWS Deep Cuts: Amazon Nova 2 Sonic")
    print("  WebSocket Relay Server")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  WebSocket: ws://{HOST}:{PORT}")
    print(f"  Region:    {REGION}")
    print(f"  Model:     {MODEL_ID}")
    print(f"  Security:  127.0.0.1 only (外部アクセス不可)")
    print("")
    _check_credentials()
    print("  index.html をブラウザで開いてください。")
    print("  Ctrl+C で停止します。")
    print("")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Windows / Unix 両対応のシグナルハンドラ
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
    else:
        loop.add_signal_handler(signal.SIGINT, stop_event.set)

    try:
        # 音声チャンク (base64) は 1 メッセージ数 KB〜数十 KB なので上限は余裕を持たせる
        server = await serve(
            handle_connection, HOST, PORT,
            origins=ALLOWED_ORIGINS,
            max_size=4 * 1024 * 1024,
        )
    except OSError as e:
        print(f"  ❌ ポート {PORT} で待ち受けできません: {e}")
        print("     → 別のターミナルで server.py / setup.py が起動したままになっていないか確認してください。")
        print("     → 既存のプロセスを停止するか、別ポートで起動してください (例: PORT=8766)。")
        print("       ※ ポートを変えた場合は index.html の Settings > Server URL も変更してください。")
        sys.exit(1)
    try:
        await stop_event.wait()
    finally:
        server.close()
        await server.wait_closed()
        print("\n[Server] Stopped — port released")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Server] Stopped")
