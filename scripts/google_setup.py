"""Мастер настройки Gmail OAuth.

Делает всё, что можно сделать из консоли:
  * открывает нужные страницы Google Cloud в браузере;
  * принимает Client ID и Client secret;
  * записывает их в .env (не в код и не в git);
  * запускает oauth-login и проверку доступа.

Запуск:
    python scripts/google_setup.py
    python scripts/google_setup.py --client-id ... --client-secret ... --email you@gmail.com
    python scripts/google_setup.py --skip-login        # только записать .env
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

PAGES = (
    "https://console.cloud.google.com/projectcreate",
    "https://console.cloud.google.com/auth/audience",
    "https://console.cloud.google.com/auth/clients",
)


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def set_env_value(key: str, value: str) -> None:
    if not ENV_PATH.exists():
        ENV_PATH.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def run(module: str, *args: str) -> int:
    return subprocess.call([sys.executable, "-m", module, *args], cwd=str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="настройка Gmail OAuth для kpbot")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--email")
    parser.add_argument("--no-browser", action="store_true", help="не открывать страницы Google Cloud")
    parser.add_argument("--skip-login", action="store_true", help="только записать значения в .env")
    args = parser.parse_args()

    if not ENV_PATH.exists():
        ENV_PATH.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        print("создан .env из шаблона")

    email = args.email or read_env().get("KPBOT_IMAP_USER", "")
    if not email:
        email = input("ваш Gmail-адрес: ").strip()

    client_id = args.client_id
    client_secret = args.client_secret

    if not client_id or not client_secret:
        print("\nОткрываю Google Cloud. Три шага в браузере:")
        print("  1) New project → имя kpbot")
        print("  2) Google Auth Platform → Get started (Audience: External)")
        print("     и Test users → добавить свой адрес")
        print("  3) Credentials → Create credentials → OAuth client ID → Desktop app")
        print()
        if not args.no_browser:
            for page in PAGES:
                webbrowser.open(page)
        if not client_id:
            client_id = input("Client ID: ").strip()
        if not client_secret:
            client_secret = getpass.getpass("Client secret (ввод скрыт): ").strip()

    if not client_id.endswith(".apps.googleusercontent.com"):
        print(f"это не Client ID: {client_id!r}")
        print("ожидается вид 1234567890-abc123.apps.googleusercontent.com")
        return 1
    if not client_secret.startswith("GOCSPX-"):
        print("предупреждение: Client secret обычно начинается с GOCSPX-")

    set_env_value("KPBOT_IMAP_USER", email)
    set_env_value("KPBOT_OAUTH_CLIENT_ID", client_id)
    set_env_value("KPBOT_OAUTH_CLIENT_SECRET", client_secret)
    print("записано в .env: KPBOT_IMAP_USER, KPBOT_OAUTH_CLIENT_ID, KPBOT_OAUTH_CLIENT_SECRET")

    if args.skip_login:
        return 0

    print()
    run("kpbot.cli", "mail-status")
    print("\nВход в Google: в браузере выберите «Дополнительно → Перейти в kpbot → Разрешить»")
    code = run("kpbot.cli", "oauth-login")
    if code != 0:
        print("вход не завершён: проверьте шаг Test users и повторите")
        return code

    print()
    return run("kpbot.cli", "mail-check")


if __name__ == "__main__":
    sys.exit(main())