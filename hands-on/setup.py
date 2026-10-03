#!/usr/bin/env python3
"""AWS Deep Cuts - Amazon Nova 2 Sonic セットアップ + サーバー起動

このスクリプトは以下を行います:
  1. Python バージョン確認 (3.12 以上)
  2. Python 依存パッケージ確認/インストール
  3. AWS 認証確認 (アクセスキー / IAM Identity Center / aws login / AssumeRole など)
  4. Nova 2 Sonic の呼び出し確認 (権限・リージョン)
  5. Nova 2 Sonic からのあいさつ (AI から一言話して終わる)
  6. WebSocket 中継サーバーの起動

前提:
  - Python 3.12+ がインストールされていること
  - AWS CLI などで認証情報が設定済みであること
    (例: aws configure / aws configure sso + aws sso login / aws login)

オプション:
  --skip-check   手順 4 の呼び出し確認と手順 5 のあいさつをスキップする
  --no-welcome   手順 5 のあいさつだけをスキップする
"""

import os
import sys
import subprocess
from pathlib import Path

# Windows コンソールの文字化け対策
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONUTF8", "1")

SCRIPT_DIR = Path(__file__).resolve().parent
REQUIREMENTS = SCRIPT_DIR / "requirements.txt"
MIN_PYTHON = (3, 12)

# (pip 名, import 名, 最低バージョン)
REQUIRED_PACKAGES = [
    ("aws-sdk-bedrock-runtime", "aws_sdk_bedrock_runtime", "0.11.0"),
    ("boto3", "boto3", "1.41.0"),
    ("websockets", "websockets", "14.2"),
    # 双方向ストリーミングに必要な HTTP/2 トランスポート (aws-sdk-bedrock-runtime[awscrt])
    ("awscrt", "awscrt", "0.20.0"),
]


def hr():
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def fail(lines):
    print()
    for line in lines:
        print(f"  {line}")
    print()
    sys.exit(1)


# ─── Step 1: Python バージョン ───────────────────────────────────
def check_python():
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info >= MIN_PYTHON:
        print(f"  Python {version} ({sys.executable}) ✓")
        return
    need = ".".join(map(str, MIN_PYTHON))
    lines = [
        f"❌ Python {need} 以上が必要です (現在: {version})",
        "   Nova 2 Sonic の双方向ストリーミングに使う aws-sdk-bedrock-runtime が",
        f"   Python {need} 以上にしか対応していないためです。",
        "",
        "   対処:",
    ]
    if sys.platform == "win32":
        lines += [
            "   → https://www.python.org/downloads/ から Python 3.12 以上をインストールし、",
            "     py -3.12 setup.py で実行してください。",
        ]
    elif sys.platform == "darwin":
        lines += ["   → brew install python@3.12 を実行し、python3.12 setup.py で実行してください。"]
    else:
        lines += ["   → python3.12 をインストールし、python3.12 setup.py で実行してください。"]
    lines += ["   → uv を使う場合: uv run --python 3.12 --with-requirements requirements.txt setup.py"]
    fail(lines)


# ─── Step 2: パッケージ ──────────────────────────────────────────
def _version_tuple(v: str):
    parts = []
    for p in v.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _missing_packages():
    from importlib import metadata

    missing = []
    for pip_name, _, min_version in REQUIRED_PACKAGES:
        try:
            installed = metadata.version(pip_name)
        except metadata.PackageNotFoundError:
            missing.append(f"{pip_name} (未インストール)")
            continue
        if _version_tuple(installed) < _version_tuple(min_version):
            missing.append(f"{pip_name} ({installed} → {min_version} 以上が必要)")
    return missing


def check_and_install_packages():
    missing = _missing_packages()
    if missing:
        print("  インストール/更新が必要:")
        for m in missing:
            print(f"    - {m}")
        print("  pip install -r requirements.txt を実行中...")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout).strip()
            lines = ["❌ パッケージのインストールに失敗しました。", ""]
            lines += ["   " + l for l in output.splitlines()[-8:]]
            lines += [""]
            if "externally-managed-environment" in output:
                lines += [
                    "   OS 管理下の Python には直接インストールできません。仮想環境を作成してください:",
                ]
            else:
                lines += ["   仮想環境 (venv) を作成してから再実行すると解決することがあります:"]
            if sys.platform == "win32":
                lines += [
                    "     py -3.12 -m venv .venv",
                    "     .venv\\Scripts\\activate",
                    "     python setup.py",
                ]
            else:
                lines += [
                    "     python3.12 -m venv .venv",
                    "     source .venv/bin/activate",
                    "     python setup.py",
                ]
            fail(lines)
        # インストール後の再確認
        still = _missing_packages()
        if still:
            fail(["❌ インストール後もパッケージを読み込めません: " + ", ".join(still)])

    for _, import_name, _ in REQUIRED_PACKAGES:
        __import__(import_name)
    print("  aws-sdk-bedrock-runtime, awscrt, boto3, websockets: OK ✓")


# ─── Step 3: 認証 ────────────────────────────────────────────────
def check_auth():
    import aws_auth

    print(f"  Profile: {aws_auth.profile_label()}")
    if aws_auth.env_keys_present():
        print("  ℹ️  環境変数 AWS_ACCESS_KEY_ID を検出しました (プロファイルより優先されます)")
    try:
        session = aws_auth.create_boto3_session()
        identity = session.client("sts").get_caller_identity()
        creds = session.get_credentials()
    except Exception as e:
        print()
        print(aws_auth.format_error_for_console(aws_auth.describe_error(e)))
        print()
        sys.exit(1)

    method = getattr(creds, "method", "unknown") if creds else "unknown"
    method_label = {
        "sso": "IAM Identity Center (SSO)",
        "login": "aws login",
        "assume-role": "AssumeRole プロファイル",
        "assume-role-with-web-identity": "Web Identity",
        "container-role": "コンテナロール (CloudShell / ECS など)",
        "iam-role": "インスタンスロール (EC2)",
        "env": "環境変数",
        "shared-credentials-file": "アクセスキー (~/.aws/credentials)",
        "config-file": "アクセスキー (~/.aws/config)",
        "custom-process": "credential_process",
    }.get(method, method)
    print(f"  Account: {identity['Account']} ✓")
    print(f"  Identity: {identity['Arn']}")
    print(f"  認証方式: {method_label}")


# ─── Step 4: Nova 2 Sonic 呼び出し確認 ───────────────────────────
async def _probe_stream():
    """双方向ストリームを実際に開き、認証・権限・リージョンのエラーが出ないか確かめる"""
    import asyncio
    import json

    import aws_auth
    from aws_sdk_bedrock_runtime.client import InvokeModelWithBidirectionalStreamOperationInput
    from aws_sdk_bedrock_runtime.models import (
        BidirectionalInputPayloadPart,
        InvokeModelWithBidirectionalStreamInputChunk,
        InvokeModelWithBidirectionalStreamOutputChunk,
    )

    client = await aws_auth.create_bedrock_runtime_client()
    stream = await asyncio.wait_for(
        client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=aws_auth.MODEL_ID)
        ),
        timeout=20,
    )
    try:
        for event in (
            {"event": {"sessionStart": {"inferenceConfiguration": {"maxTokens": 16, "topP": 0.9, "temperature": 0.7}}}},
            {"event": {"sessionEnd": {}}},
        ):
            await stream.input_stream.send(
                InvokeModelWithBidirectionalStreamInputChunk(
                    value=BidirectionalInputPayloadPart(bytes_=json.dumps(event).encode("utf-8"))
                )
            )
        await stream.input_stream.close()
        # 認証・権限エラーはここで例外になる
        _, output = await asyncio.wait_for(stream.await_output(), timeout=20)
        try:
            result = await asyncio.wait_for(output.receive(), timeout=5)
        except asyncio.TimeoutError:
            return
        if result is not None and not isinstance(result, InvokeModelWithBidirectionalStreamOutputChunk):
            value = getattr(result, "value", None)
            if isinstance(value, BaseException):
                raise value
    finally:
        try:
            await stream.close()
        except Exception:
            pass


def check_model():
    import asyncio

    import aws_auth

    region = aws_auth.resolve_region()
    if region not in aws_auth.SUPPORTED_REGIONS:
        print(f"  ⚠️  {region} は Nova 2 Sonic の対応リージョンではない可能性があります")
        print(f"     対応: {', '.join(aws_auth.SUPPORTED_REGIONS)}")

    async def _main():
        aws_auth.install_quiet_exception_handler(asyncio.get_running_loop())
        await _probe_stream()

    try:
        asyncio.run(_main())
    except asyncio.TimeoutError:
        print("  ⚠️  応答がタイムアウトしました (ネットワークが遅い可能性)。起動は継続します。")
        return
    except Exception as e:
        info = aws_auth.describe_error(e)
        # 設定値の検証エラーなどは「認証・権限は通っている」ので問題なし
        if info["category"] in ("auth", "permission", "region", "network"):
            print()
            print(aws_auth.format_error_for_console(info))
            print()
            if info["category"] == "region":
                if sys.platform == "win32":
                    print('  リージョンを変更する場合 (PowerShell): $env:AWS_REGION="us-east-1"; python setup.py')
                else:
                    print("  リージョンを変更する場合: AWS_REGION=us-east-1 python3 setup.py")
                print()
            sys.exit(1)
        if os.environ.get("DEBUG"):
            print(f"  (参考: {info['code']}: {info['detail']})")
    print(f"  {aws_auth.MODEL_ID} @ {region}: OK ✓")


def main():
    skip_check = "--skip-check" in sys.argv[1:]
    skip_welcome = skip_check or "--no-welcome" in sys.argv[1:]

    print()
    hr()
    print("  AWS Deep Cuts - Amazon Nova 2 Sonic")
    hr()
    print()

    print("Step 1/6: Python バージョンを確認")
    check_python()
    print()

    # aws_auth を import できるようにする (どのディレクトリから実行されても動くように)
    sys.path.insert(0, str(SCRIPT_DIR))
    import aws_auth

    print(f"  Region: {aws_auth.resolve_region()}")
    print(f"  Model:  {aws_auth.MODEL_ID}")
    print()

    print("Step 2/6: Python パッケージを確認")
    check_and_install_packages()
    print()

    print("Step 3/6: AWS 認証を確認")
    check_auth()
    print()

    print("Step 4/6: Nova 2 Sonic の呼び出しを確認")
    if skip_check:
        print("  --skip-check が指定されたためスキップしました")
    else:
        check_model()
    print()

    print("Step 5/6: Nova 2 Sonic からのあいさつ")
    if skip_welcome:
        print("  スキップしました")
    else:
        import welcome
        welcome.run_welcome()
    print()

    print("Step 6/6: WebSocket 中継サーバーを起動")
    print()
    hr()
    print("  セットアップ完了")
    hr()
    print()
    print("  サーバーを起動します。")
    print("  起動したら index.html をブラウザで開いてください。")
    print()
    print("  停止: Ctrl+C")
    print()

    os.chdir(SCRIPT_DIR)
    os.environ["AWS_REGION"] = aws_auth.resolve_region()
    proc = subprocess.Popen([sys.executable, str(SCRIPT_DIR / "server.py")])
    try:
        proc.wait()
    except KeyboardInterrupt:
        # Ctrl+C を子プロセスに伝播（子プロセスも SIGINT を受ける）
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\n[setup] Server stopped")


if __name__ == "__main__":
    main()
