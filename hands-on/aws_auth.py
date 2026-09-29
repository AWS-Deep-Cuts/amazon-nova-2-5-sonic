"""
AWS 認証ヘルパー — setup.py / server.py 共通

Nova 2 Sonic の双方向ストリーミング API は、boto3 ではなく新しい Python SDK
(aws-sdk-bedrock-runtime / smithy-python) からしか呼び出せません。
この SDK の標準クレデンシャルチェーンは、環境変数・静的キー・credential_process
にしか対応しておらず、以下の認証方式ではそのままだと失敗します。

  - IAM Identity Center (AWS SSO) プロファイル (`aws configure sso`)
  - `aws login` (コンソール認証情報でのサインイン)
  - AssumeRole プロファイル (role_arn + source_profile)
  - CloudShell / ECS / EC2 などのコンテナ・インスタンスロール

そこで、あらゆる認証方式に対応済みの boto3 (botocore) のクレデンシャル解決を
smithy-python の IdentityResolver として橋渡しします。
あわせて、認証・権限まわりのエラーを「何が起きていて、何をすればよいか」
の形に変換する関数を提供します。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

# このハンズオンで利用するリージョン / モデル
DEFAULT_REGION = "ap-northeast-1"
SUPPORTED_REGIONS = ("us-east-1", "us-west-2", "ap-northeast-1", "eu-north-1")
MODEL_ID = "amazon.nova-2-sonic-v1:0"


def resolve_region() -> str:
    """AWS_REGION → AWS_DEFAULT_REGION → ap-northeast-1 の順でリージョンを決める

    プロファイルの region はあえて使いません。Identity Center のプロファイルは
    Nova 2 Sonic 非対応リージョン (例: ap-northeast-3) が設定されていることが多く、
    ハンズオンの再現性が落ちるためです。
    """
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def profile_label() -> str:
    """現在使われるプロファイル名 (表示用)"""
    return os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE") or "default"


def env_keys_present() -> bool:
    """環境変数のアクセスキーが設定されているか (プロファイルより優先されるため要注意)"""
    return bool(os.environ.get("AWS_ACCESS_KEY_ID"))


def create_boto3_session():
    import boto3

    return boto3.Session(region_name=resolve_region())


# ─── smithy-python 用 IdentityResolver ─────────────────────────────
def create_credentials_resolver(session=None):
    """boto3 のクレデンシャルを返す smithy-python の IdentityResolver を生成する

    botocore の RefreshableCredentials は有効期限が近づくと自動で更新されるため、
    SSO / AssumeRole などの一時クレデンシャルでも長時間の利用に耐えます。
    """
    from smithy_aws_core.identity import AWSCredentialsIdentity
    from smithy_core.aio.interfaces.identity import IdentityResolver

    boto_session = session or create_boto3_session()

    class BotocoreCredentialsResolver(IdentityResolver):
        def __init__(self) -> None:
            self._credentials = None

        async def get_identity(self, *, properties: Any) -> AWSCredentialsIdentity:
            if self._credentials is None:
                # SSO トークンの読み込みなどブロッキング I/O があるためスレッドで実行
                self._credentials = await asyncio.to_thread(boto_session.get_credentials)
                if self._credentials is None:
                    from botocore.exceptions import NoCredentialsError

                    raise NoCredentialsError()
            frozen = await asyncio.to_thread(self._credentials.get_frozen_credentials)
            expiry = getattr(self._credentials, "_expiry_time", None)
            return AWSCredentialsIdentity(
                access_key_id=frozen.access_key,
                secret_access_key=frozen.secret_key,
                session_token=frozen.token,
                expiration=expiry,
            )

    return BotocoreCredentialsResolver()


async def create_bedrock_runtime_client():
    """Nova 2 Sonic 用の AsyncBedrockRuntimeClient を生成する

    - 認証: boto3 のクレデンシャル解決を使う (Identity Center / aws login などに対応)
    - 通信: AWS CRT の HTTP クライアントを使う。aws-sdk-bedrock-runtime 0.11 以降の
      既定 (aiohttp) は双方向ストリーミングに非対応で、UnsupportedTransportError になる。
    """
    from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
    from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
    from smithy_http.aio.crt import AWSCRTHTTPClient

    config = await AsyncBedrockRuntimeConfig.resolve(
        region=resolve_region(),
        aws_credentials_identity_resolver=create_credentials_resolver(),
        transport=AWSCRTHTTPClient(),
    )
    return AsyncBedrockRuntimeClient(config=config)


# ─── エラーの分類 ───────────────────────────────────────────────────
def _sso_hint() -> list[str]:
    profile = profile_label()
    opt = "" if profile == "default" else f" --profile {profile}"
    return [f"aws sso login{opt} を実行して再ログインしてください。"]


def _login_hint() -> list[str]:
    profile = profile_label()
    opt = "" if profile == "default" else f" --profile {profile}"
    return [
        f"IAM Identity Center (SSO) の場合: aws sso login{opt}",
        f"aws login を使っている場合:        aws login{opt}",
        "アクセスキーの場合:               aws configure で再設定",
    ]


def _profile_hint() -> list[str]:
    if os.name == "nt":
        return ['プロファイルを指定する場合 (PowerShell): $env:AWS_PROFILE="プロファイル名"']
    return ["プロファイルを指定する場合: export AWS_PROFILE=プロファイル名"]


def _env_key_warning() -> list[str]:
    if not env_keys_present():
        return []
    return [
        "環境変数 AWS_ACCESS_KEY_ID が設定されています。環境変数はプロファイル (SSO 含む) より"
        "優先されます。Identity Center のポータルからコピーした一時キーは数時間で失効するため、"
        "不要なら AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN を削除してください。"
    ]


def describe_error(exc: BaseException) -> dict:
    """例外を {code, title, detail, hints} に変換する"""
    name = type(exc).__name__
    text = str(exc) or name
    # botocore の ClientError はエラーコードを持つ
    code = name
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", name)
    haystack = f"{code} {name} {text}"

    def result(title: str, hints: list[str], category: str) -> dict:
        return {"code": code, "category": category, "title": title, "detail": text, "hints": hints}

    # ── 入力設定の誤り (server.py 側の検証)
    if name == "ValueError":
        return result(text, ["Settings の内容を確認し、分からなければ Reset してください。"], "config")

    # ── 認証情報が見つからない / プロファイルが無い
    if name == "ProfileNotFound":
        return result(
            f"プロファイル '{profile_label()}' が見つかりません。",
            ["aws configure list-profiles で設定済みのプロファイル名を確認してください。"] + _profile_hint(), "auth")
    if name in ("NoCredentialsError", "PartialCredentialsError", "IdentityChainError"):
        return result(
            "AWS の認証情報が見つかりません。",
            [
                "IAM Identity Center (SSO) を使う場合: aws configure sso → aws sso login --profile プロファイル名",
                "aws login を使う場合: aws login",
                "アクセスキーを使う場合: aws configure",
            ]
            + _profile_hint(), "auth")

    # ── SSO / aws login のトークン切れ
    if name in ("UnauthorizedSSOTokenError", "SSOTokenLoadError") or (
        name == "TokenRetrievalError" and "sso" in text.lower()
    ):
        return result("IAM Identity Center (SSO) のセッションが切れています。", _sso_hint(), "auth")
    if name.startswith("Login") or (name == "TokenRetrievalError"):
        return result("サインインセッションが切れています。", _login_hint(), "auth")
    if name == "SSOError" or "sso" in name.lower():
        return result("IAM Identity Center (SSO) の認証に失敗しました。", _sso_hint(), "auth")
    if name == "CredentialRetrievalError":
        return result(
            "credential_process などの外部コマンドから認証情報を取得できませんでした。",
            ["~/.aws/config の credential_process の設定と、そのコマンド単体での動作を確認してください。"], "auth")

    # ── クレデンシャルが無効・期限切れ
    if any(k in haystack for k in ("ExpiredToken", "RequestExpired", "security token included in the request is expired")):
        return result("一時的な認証情報の有効期限が切れています。", _env_key_warning() + _login_hint(), "auth")
    if any(k in haystack for k in ("UnrecognizedClient", "InvalidClientTokenId", "security token included in the request is invalid")):
        return result("認証情報が無効です (アクセスキーが削除された・誤っている可能性)。", _env_key_warning() + _login_hint(), "auth")
    if "SignatureDoesNotMatch" in haystack:
        return result(
            "リクエストの署名が一致しません (シークレットキーの誤り、または PC の時刻ずれ)。",
            ["aws configure で再設定するか、PC の時刻同期を確認してください。"] + _env_key_warning(), "auth")

    # ── 権限不足
    if "AccessDenied" in haystack or "not authorized" in text:
        return result(
            "IAM の権限が不足しています。",
            [
                "必要な権限: bedrock:InvokeModel"
                f" (対象: arn:aws:bedrock:{resolve_region()}::foundation-model/{MODEL_ID})。"
                "双方向ストリーミング API もこのアクションで認可されます。",
                "IAM Identity Center の場合は、許可セット (Permission set) にこの権限を追加し、"
                "アカウントへ再プロビジョニングしてください。",
                "SCP や許可の境界 (Permissions boundary) で Bedrock が拒否されていないかも確認してください。",
            ],
        )

    # ── リージョン・モデル
    if any(k in haystack for k in ("ResourceNotFound", "model identifier is invalid", "isn't supported", "not supported in this region")):
        return result(
            f"リージョン {resolve_region()} では {MODEL_ID} を利用できません。",
            [f"対応リージョン: {', '.join(SUPPORTED_REGIONS)}", "環境変数 AWS_REGION で変更してください。"], "region")
    if "ValidationException" in haystack:
        return result(
            "リクエストの内容が不正です。Settings の Session Config / Prompt Config を見直してください。",
            ["voiceId、sampleRateHertz、endpointingSensitivity などの値を確認し、分からなければ Reset してください。"], "config")

    # ── スロットリング・サービス側
    if any(k in haystack for k in ("Throttling", "ServiceQuota", "TooManyRequests")):
        return result(
            "リクエスト数の上限に達しました。",
            ["少し時間を置いてから再度 Start してください。", "同時セッション数のクォータは Service Quotas で確認できます。"], "service")
    if any(k in haystack for k in ("ModelTimeout", "timed out", "Timeout")):
        return result(
            "Nova 2 Sonic のセッションがタイムアウトしました。",
            ["無音が長く続く、または 1 接続の上限時間 (8 分) に達すると切断されます。Start し直してください。"], "timeout")
    if any(k in haystack for k in ("ServiceUnavailable", "InternalServer", "ModelStreamError")):
        return result("Bedrock 側で一時的なエラーが発生しました。", ["少し時間を置いてから再度 Start してください。"], "service")

    # ── ネットワーク
    if any(k in haystack for k in ("EndpointConnectionError", "ConnectTimeout", "ClientConnectorError", "Cannot connect", "Name or service not known", "getaddrinfo")):
        return result(
            "AWS に接続できません。",
            ["ネットワーク・プロキシ・VPN の設定を確認してください。", "社内プロキシ環境では HTTPS_PROXY の設定が必要な場合があります。"], "network")

    return result("予期しないエラーが発生しました。", ["server.py を実行しているターミナルのログも確認してください。"], "unknown")


def format_error_for_console(info: dict) -> str:
    lines = [f"  ❌ {info['title']}"]
    for hint in info["hints"]:
        lines.append(f"     → {hint}")
    lines.append(f"     (詳細: {info['code']}: {info['detail']})")
    return "\n".join(lines)
