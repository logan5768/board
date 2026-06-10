"""
Spirt AI-доска — всё локально, без внешних серверов.

Двойной клик по start-proxy.bat (или `py proxy.py`):
  1) поднимает локальный сервер на http://localhost:8787
  2) раздаёт index.html — саму доску
  3) на POST /v1/messages отвечает САМ: вызывает локальный Claude Code
     (claude.exe, авторизация по вашей подписке — `claude /login` один раз)
     и возвращает ответ в формате Anthropic Messages API
  4) сам открывает доску в браузере

Никакого внешнего API и ключей не нужно: мост работает через подписку,
которой авторизован Claude Code на этой машине.

Аргументы (необязательно):
    py proxy.py 9000                                  # другой порт
    py proxy.py 8787 --upstream https://api.zenoid.space
        # старый режим: вместо моста проксировать /v1/* на внешний
        # Anthropic-совместимый эндпоинт (нужен ключ в настройках доски)
"""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "origin", "referer",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, PATCH",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
    "Access-Control-Max-Age": "86400",
}

# ---------------------------------------------------------------- claude CLI

def find_claude():
    """Ищем claude.exe: PATH, затем типичные места установки на Windows."""
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None

CLAUDE_BIN = find_claude()

# Метка, которой модель помечает вызов инструмента create_card (см. ниже).
TOOL_MARKER = "CREATE_CARD>>>"

TOOL_INSTRUCTIONS = (
    "\n\nУ тебя есть инструмент create_card(title, content) — создаёт новую "
    "карточку на доске идей.\nЕсли решишь создать карточку, добавь В САМОМ "
    "КОНЦЕ ответа отдельную строку строго такого вида:\n"
    + TOOL_MARKER + '{"title":"Заголовок","content":"Текст карточки"}\n'
    "Не упоминай этот синтаксис в видимом тексте ответа, не оборачивай его в "
    "``` и не делай больше одного вызова за ответ."
)

BRIDGE_SYSTEM = (
    "Ты — ИИ-ассистент чата, встроенного в доску идей. Тебе передают "
    "инструкции и историю диалога; ответь ТОЛЬКО на последнее сообщение "
    "пользователя, без префиксов вроде \"Ассистент:\"."
)


def pick_model(raw):
    """Имя модели из запроса -> алиас, который понимает claude CLI."""
    m = (raw or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"


def blocks_to_text(content, role):
    """Содержимое сообщения (строка или список блоков) -> плоский текст."""
    if isinstance(content, str):
        return content.strip()
    parts = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_use":
            parts.append("[вызвал инструмент %s(%s)]" % (
                b.get("name", "?"), json.dumps(b.get("input", {}), ensure_ascii=False)))
        elif t == "tool_result":
            inner = b.get("content")
            if isinstance(inner, list):
                inner = "\n".join(x.get("text", "") for x in inner if x.get("type") == "text")
            parts.append("[результат инструмента: %s]" % (inner or ""))
    return "\n".join(p for p in parts if p).strip()


def build_prompt(payload):
    """Anthropic-запрос -> один текстовый промпт для claude -p."""
    parts = []
    system = payload.get("system") or ""
    tools = payload.get("tools") or []
    instructions = system
    if any(t.get("name") == "create_card" for t in tools):
        instructions += TOOL_INSTRUCTIONS
    if instructions.strip():
        parts.append("<инструкции>\n" + instructions.strip() + "\n</инструкции>")
    lines = []
    for m in payload.get("messages") or []:
        who = "Пользователь" if m.get("role") == "user" else "Ассистент"
        text = blocks_to_text(m.get("content"), m.get("role"))
        if text:
            lines.append("%s: %s" % (who, text))
    parts.append("<диалог>\n" + "\n\n".join(lines) + "\n</диалог>")
    parts.append("Ответь на последнее сообщение пользователя.")
    return "\n\n".join(parts)


def split_tool_call(text):
    """Вырезаем строку CREATE_CARD>>>{...} из ответа -> (текст, input|None)."""
    idx = text.rfind(TOOL_MARKER)
    if idx == -1:
        return text.strip(), None
    head = text[:idx]
    tail = text[idx + len(TOOL_MARKER):].strip()
    # Срезаем возможный мусор после JSON (берём до последней '}').
    end = tail.rfind("}")
    if end != -1:
        tail = tail[:end + 1]
    try:
        tool_input = json.loads(tail)
    except ValueError:
        return text.strip(), None
    if not isinstance(tool_input, dict):
        return head.strip(), None
    return head.strip(), tool_input


# ----------------------------------------------------------------- auth

class NotLoggedIn(RuntimeError):
    pass


def auth_status():
    """`claude auth status --json` -> {'loggedIn': bool, 'authMethod': ...}"""
    if not CLAUDE_BIN:
        return {"loggedIn": False, "authMethod": "none", "error": "claude.exe не найден"}
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "auth", "status", "--json"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=30, cwd=ROOT,
        )
        return json.loads((proc.stdout or "").strip() or "{}")
    except Exception as e:
        return {"loggedIn": False, "authMethod": "unknown", "error": str(e)}


def auth_login():
    """Запускает OAuth-вход через браузер в отдельном консольном окне."""
    if not CLAUDE_BIN:
        raise RuntimeError("claude.exe не найден — установите Claude Code")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
    subprocess.Popen([CLAUDE_BIN, "auth", "login", "--claudeai"],
                     creationflags=flags, cwd=ROOT)


def auth_logout():
    if not CLAUDE_BIN:
        raise RuntimeError("claude.exe не найден")
    proc = subprocess.run(
        [CLAUDE_BIN, "auth", "logout"],
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=30, cwd=ROOT,
    )
    return (proc.stdout or proc.stderr or "").strip()


_tool_seq = [0]

def call_claude(payload):
    """Вызывает claude -p и возвращает ответ в формате Anthropic Messages."""
    if not CLAUDE_BIN:
        raise RuntimeError(
            "claude.exe не найден. Установите Claude Code "
            "(https://claude.com/claude-code) и выполните `claude /login`.")
    model = pick_model(payload.get("model"))
    prompt = build_prompt(payload)
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--model", model,
        "--system-prompt", BRIDGE_SYSTEM,
    ]
    started = time.time()
    sys.stderr.write("[bridge] -> claude: model=%s, промпт %d символов\n" % (model, len(prompt)))
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True,
        encoding="utf-8", errors="replace", timeout=300, cwd=ROOT,
    )
    elapsed = time.time() - started
    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out)
    except ValueError:
        sys.stderr.write("[bridge] ОШИБКА за %.1fс, код %s\nstdout: %s\nstderr: %s\n" % (
            elapsed, proc.returncode, out[:400], (proc.stderr or "")[:400]))
        raise RuntimeError(
            "claude вернул не-JSON (код %s): %s" % (
                proc.returncode, (out or proc.stderr or "")[:400]))
    sys.stderr.write("[bridge] <- ответ за %.1fс: is_error=%s, %d символов\n" % (
        elapsed, data.get("is_error"), len(data.get("result") or "")))
    result_text = data.get("result") or ""
    if data.get("is_error"):
        if "Not logged in" in result_text:
            raise NotLoggedIn(
                "Claude не подключён. Нажмите «Войти» в чате — откроется "
                "страница входа в аккаунт с подпиской.")
        raise RuntimeError("claude: " + (result_text or "неизвестная ошибка"))

    text, tool_input = split_tool_call(result_text)
    content = []
    if text:
        content.append({"type": "text", "text": text})
    stop_reason = "end_turn"
    if tool_input is not None:
        _tool_seq[0] += 1
        content.append({
            "type": "tool_use",
            "id": "toolu_local_%d_%d" % (int(started), _tool_seq[0]),
            "name": "create_card",
            "input": tool_input,
        })
        stop_reason = "tool_use"
    if not content:
        content.append({"type": "text", "text": "(пустой ответ)"})
    usage = data.get("usage") or {}
    return {
        "id": data.get("session_id", "msg_local"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    }

# ------------------------------------------------------------------- server

def make_handler(upstream_url):
    """upstream_url=None -> мост через claude CLI; иначе старый прокси."""
    if upstream_url:
        parts = urlsplit(upstream_url)
        upstream_host = parts.hostname
        upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
        upstream_scheme = parts.scheme

    class Handler(SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __init__(self, *args, **kwargs):
            # Статику раздаём из каталога этого файла (там лежит index.html).
            super().__init__(*args, directory=ROOT, **kwargs)

        def log_message(self, fmt, *args):
            sys.stderr.write("[board] " + (fmt % args) + "\n")

        def _send_cors(self):
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)

        def _is_api(self):
            return self.path == "/v1" or self.path.startswith("/v1/")

        def _send_json(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._send_cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path.rstrip("/") == "/auth/status":
                self._send_json(200, auth_status())
            elif self._is_api():
                if upstream_url:
                    self._forward()
                else:
                    self._send_json(404, {"error": {"message": "локальный мост поддерживает только POST /v1/messages"}})
            else:
                super().do_GET()

        def do_HEAD(self):
            if self._is_api() and upstream_url:
                self._forward()
            else:
                super().do_HEAD()

        def do_POST(self):
            path = self.path.rstrip("/")
            if path == "/auth/login":
                try:
                    auth_login()
                    self._send_json(200, {"started": True})
                except Exception as e:
                    self._send_json(500, {"error": {"message": str(e)}})
                return
            if path == "/auth/logout":
                try:
                    self._send_json(200, {"ok": True, "detail": auth_logout()})
                except Exception as e:
                    self._send_json(500, {"error": {"message": str(e)}})
                return
            if upstream_url:
                self._forward()
                return
            if path != "/v1/messages":
                self._send_json(404, {"error": {"message": "неизвестный путь: " + self.path}})
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                self._send_json(400, {"error": {"message": "невалидный JSON в теле запроса"}})
                return
            try:
                resp = call_claude(payload)
            except NotLoggedIn as e:
                self._send_json(401, {"error": {"type": "not_logged_in", "message": str(e)}})
                return
            except subprocess.TimeoutExpired:
                self._send_json(504, {"error": {"message": "claude не ответил за 5 минут"}})
                return
            except Exception as e:
                self._send_json(502, {"error": {"message": str(e)}})
                return
            self._send_json(200, resp)

        def do_PUT(self):
            self._maybe_forward()

        def do_DELETE(self):
            self._maybe_forward()

        def do_PATCH(self):
            self._maybe_forward()

        def _maybe_forward(self):
            if upstream_url:
                self._forward()
            else:
                self._send_json(405, {"error": {"message": "метод не поддерживается локальным мостом"}})

        def _forward(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else None

            fwd_headers = {}
            for k, v in self.headers.items():
                if k.lower() in HOP_BY_HOP:
                    continue
                fwd_headers[k] = v
            fwd_headers["Host"] = upstream_host

            if upstream_scheme == "https":
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection(upstream_host, upstream_port, context=ctx, timeout=120)
            else:
                conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=120)

            try:
                conn.request(self.command, self.path, body=body, headers=fwd_headers)
                resp = conn.getresponse()
                data = resp.read()
            except Exception as e:
                msg = f'{{"error":"proxy upstream failed: {e}"}}'.encode("utf-8")
                self.send_response(502)
                self._send_cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            finally:
                conn.close()

            self.send_response(resp.status, resp.reason)
            self._send_cors()
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP:
                    continue
                # CORS-заголовки уже выставили — не дублируем
                if k.lower().startswith("access-control-"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main():
    port = 8787
    upstream = None
    args = sys.argv[1:]
    if "--upstream" in args:
        i = args.index("--upstream")
        upstream = args[i + 1].rstrip("/")
        del args[i:i + 2]
    if args:
        port = int(args[0])

    handler = make_handler(upstream)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://localhost:{port}/"

    print(f"Spirt AI-доска:   {url}")
    if upstream:
        print(f"API-прокси /v1/*  ->  {upstream}")
    else:
        if CLAUDE_BIN:
            print(f"Чат и диаграммы   ->  локальный Claude Code ({CLAUDE_BIN})")
            st = auth_status()
            if st.get("loggedIn"):
                print(f"Авторизация: вход выполнен ({st.get('authMethod', '?')})")
            else:
                print("Авторизация: НЕ выполнена — нажмите «Войти» в чате на доске.")
        else:
            print("ВНИМАНИЕ: claude.exe не найден — чат и диаграммы работать не будут.")
            print("Установите Claude Code и выполните `claude /login`.")
    print("Закрыть — Ctrl+C или просто закройте это окно.")

    # Открываем доску в браузере чуть позже, когда сервер уже слушает.
    threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
