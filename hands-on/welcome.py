"""
セットアップ完了時のあいさつ — Nova 2 Sonic から一言だけ話しかける

対話はせず、以下の流れで「AI から話し始めて、勝手に終わる」ようにしています。

  1. システムプロンプトで「話す内容」を決める
  2. 音声入力ストリームを開き、無音を流しておく (ミュートしたマイクの代わり)
  3. Cross-modal input (USER ロールのテキスト) を 1 回送り、話し始める合図にする
     ※ Nova 2 Sonic はユーザー入力が無いと話し始めない
  4. AI の発話 (contentEnd: END_TURN) を受け取ったらセッションを閉じる
  5. 受け取った音声を WAV に保存し、OS 標準のプレイヤーで再生する

追加パッケージは不要です (再生は afplay / PowerShell / aplay などを利用)。

単体での実行:
  python welcome.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import uuid
import wave
from pathlib import Path

import aws_auth

OUTPUT_SAMPLE_RATE = 24000
INPUT_SAMPLE_RATE = 16000
SILENCE_CHUNK_SEC = 0.032  # 32ms ごとに無音を送る
WELCOME_TIMEOUT_SEC = 30

WELCOME_LINE = "Welcome to AWS Deep Cuts! Let's dig into Amazon Nova 2 Sonic together."
SYSTEM_PROMPT = (
    "You are the voice of AWS Deep Cuts, a tech series that digs deep into hidden gems of AWS. "
    "When you receive any message, say exactly the following sentence in a cheerful, energetic tone, "
    f'and nothing else: "{WELCOME_LINE}" '
    "Do not ask any questions and do not add anything before or after it."
)

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output" / "welcome.wav"


async def generate_welcome(voice_id: str = "matthew") -> tuple[bytes, str]:
    """Nova 2 Sonic にあいさつを話させ、(PCM 音声, 書き起こし) を返す"""
    from aws_sdk_bedrock_runtime.client import InvokeModelWithBidirectionalStreamOperationInput
    from aws_sdk_bedrock_runtime.models import (
        BidirectionalInputPayloadPart,
        InvokeModelWithBidirectionalStreamInputChunk,
        InvokeModelWithBidirectionalStreamOutputChunk,
    )

    client = await aws_auth.create_bedrock_runtime_client()
    stream = await client.invoke_model_with_bidirectional_stream(
        InvokeModelWithBidirectionalStreamOperationInput(model_id=aws_auth.MODEL_ID)
    )

    prompt_name = str(uuid.uuid4())
    audio_content = str(uuid.uuid4())
    send_lock = asyncio.Lock()

    async def send(event: dict):
        async with send_lock:
            await stream.input_stream.send(
                InvokeModelWithBidirectionalStreamInputChunk(
                    value=BidirectionalInputPayloadPart(bytes_=json.dumps(event).encode("utf-8"))
                )
            )

    async def send_text(role: str, text: str, interactive: bool):
        cn = str(uuid.uuid4())
        await send({"event": {"contentStart": {
            "promptName": prompt_name, "contentName": cn, "type": "TEXT",
            "interactive": interactive, "role": role,
            "textInputConfiguration": {"mediaType": "text/plain"},
        }}})
        await send({"event": {"textInput": {"promptName": prompt_name, "contentName": cn, "content": text}}})
        await send({"event": {"contentEnd": {"promptName": prompt_name, "contentName": cn}}})

    # 1. セッション・プロンプト・システムプロンプト
    await send({"event": {"sessionStart": {
        "inferenceConfiguration": {"maxTokens": 256, "topP": 0.9, "temperature": 0.3},
    }}})
    await send({"event": {"promptStart": {
        "promptName": prompt_name,
        "textOutputConfiguration": {"mediaType": "text/plain"},
        "audioOutputConfiguration": {
            "mediaType": "audio/lpcm", "sampleRateHertz": OUTPUT_SAMPLE_RATE,
            "sampleSizeBits": 16, "channelCount": 1, "voiceId": voice_id,
            "encoding": "base64", "audioType": "SPEECH",
        },
    }}})
    await send_text("SYSTEM", SYSTEM_PROMPT, interactive=False)

    # 2. 音声入力ストリームを開き、無音を流し続ける (ミュートしたマイク相当)
    await send({"event": {"contentStart": {
        "promptName": prompt_name, "contentName": audio_content, "type": "AUDIO",
        "interactive": True, "role": "USER",
        "audioInputConfiguration": {
            "mediaType": "audio/lpcm", "sampleRateHertz": INPUT_SAMPLE_RATE,
            "sampleSizeBits": 16, "channelCount": 1, "audioType": "SPEECH", "encoding": "base64",
        },
    }}})
    silence = base64.b64encode(bytes(int(INPUT_SAMPLE_RATE * SILENCE_CHUNK_SEC) * 2)).decode()

    async def stream_silence():
        while True:
            await send({"event": {"audioInput": {
                "promptName": prompt_name, "contentName": audio_content, "content": silence,
            }}})
            await asyncio.sleep(SILENCE_CHUNK_SEC)

    silence_task = asyncio.create_task(stream_silence())

    # 3. 話し始める合図 (Cross-modal input)
    await send_text("USER", "hi", interactive=True)

    # 4. AI の発話を受け取る
    audio = bytearray()
    transcript: list[str] = []
    speculative = False
    role = ""
    try:
        _, output = await stream.await_output()

        async def receive_until_done():
            nonlocal speculative, role
            while True:
                result = await output.receive()
                if result is None:
                    return
                if not isinstance(result, InvokeModelWithBidirectionalStreamOutputChunk):
                    value = getattr(result, "value", None)
                    raise value if isinstance(value, BaseException) else RuntimeError(type(result).__name__)
                if not (result.value and result.value.bytes_):
                    continue
                evt = json.loads(result.value.bytes_.decode("utf-8")).get("event", {})
                if "contentStart" in evt:
                    cs = evt["contentStart"]
                    role = cs.get("role", "")
                    fields = cs.get("additionalModelFields")
                    try:
                        fields = json.loads(fields) if isinstance(fields, str) else (fields or {})
                    except json.JSONDecodeError:
                        fields = {}
                    speculative = fields.get("generationStage") == "SPECULATIVE"
                elif "textOutput" in evt:
                    if role == "ASSISTANT" and speculative:
                        transcript.append(evt["textOutput"].get("content", ""))
                elif "audioOutput" in evt:
                    audio.extend(base64.b64decode(evt["audioOutput"].get("content", "")))
                elif "contentEnd" in evt:
                    ce = evt["contentEnd"]
                    # AI の音声が 1 ターン話し終わったら終了
                    if audio and ce.get("type") == "AUDIO" and ce.get("stopReason") == "END_TURN":
                        return
                elif "completionEnd" in evt and audio:
                    return

        await asyncio.wait_for(receive_until_done(), timeout=WELCOME_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        if not audio:
            raise
        # 音声を受け取れていれば、終了イベントが来なくてもそのまま使う
    finally:
        silence_task.cancel()
        # 5. セッションを閉じる (失敗しても音声は使えるので無視)
        try:
            await asyncio.wait_for(_close(send, stream, prompt_name, audio_content), timeout=5)
        except (Exception, asyncio.TimeoutError):
            pass

    return bytes(audio), "".join(transcript).strip()


async def _close(send, stream, prompt_name: str, audio_content: str):
    await send({"event": {"contentEnd": {"promptName": prompt_name, "contentName": audio_content}}})
    await send({"event": {"promptEnd": {"promptName": prompt_name}}})
    await send({"event": {"sessionEnd": {}}})
    await stream.input_stream.close()


def save_wav(pcm: bytes, path: Path = DEFAULT_OUTPUT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(OUTPUT_SAMPLE_RATE)
        wf.writeframes(pcm)
    return path


def play_wav(path: Path) -> bool:
    """OS 標準のプレイヤーで WAV を再生する。再生できたら True"""
    if sys.platform == "darwin":
        candidates = [["afplay", str(path)]]
    elif sys.platform == "win32":
        quoted = str(path).replace("'", "''")  # PowerShell の単一引用符エスケープ
        ps = f"(New-Object Media.SoundPlayer '{quoted}').PlaySync()"
        candidates = [["powershell", "-NoProfile", "-Command", ps]]
    else:
        candidates = [
            ["paplay", str(path)],
            ["pw-play", str(path)],
            ["aplay", "-q", str(path)],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
        ]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            if subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def run_welcome() -> bool:
    """あいさつを生成して再生する。失敗してもハンズオンは続けられるので例外は投げない"""
    async def _main():
        aws_auth.install_quiet_exception_handler(asyncio.get_running_loop())
        return await generate_welcome()

    try:
        pcm, text = asyncio.run(_main())
    except Exception as e:
        info = aws_auth.describe_error(e)
        print("  ⚠️  あいさつ音声を生成できませんでした (ハンズオンは続行できます)")
        print(aws_auth.format_error_for_console(info))
        return False

    print(f"  🔊 Nova 2 Sonic: \"{text or WELCOME_LINE}\"")
    path = save_wav(pcm)
    if not play_wav(path):
        print(f"  (この環境では自動再生できませんでした。音声ファイル: {path})")
    return True


if __name__ == "__main__":
    sys.exit(0 if run_welcome() else 1)
