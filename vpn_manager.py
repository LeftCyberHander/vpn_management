#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPN 订阅管理器 for Clash Party (mihomo-party)
=============================================
统一管理多个机场(VPN)订阅：选择机场 → 自动更新订阅 → 写入 Clash Party 配置。
支持两种机场模式：
  A. 订阅链接模式：直接粘贴订阅地址（如扬帆云），定时/手动拉取即可
  B. 账号登录模式：填面板账号密码（如飞鸟云），自动登录获取最新订阅链接

用法：
  python vpn_manager.py                    # 交互式菜单（无参数时进入）
  python vpn_manager.py list               # 查看所有机场状态（流量/到期/上次更新）
  python vpn_manager.py add                # 添加新机场（向导）
  python vpn_manager.py update <名称>      # 更新指定机场订阅到 Clash Party
  python vpn_manager.py update all         # 更新全部机场
  python vpn_manager.py switch <名称>      # 切换 Clash Party 当前使用的机场
  python vpn_manager.py test <名称>        # 只测试订阅是否可用，不写任何文件
  python vpn_manager.py remove <名称>      # 从管理器移除机场（不动已生成的配置）

环境变量（可选）：
  VPN_CONFIG_PATH   账号配置文件路径（默认：脚本目录下 vpn_accounts.json）
  VPN_PARTY_DIR     Clash Party 配置目录（默认：%APPDATA%\\mihomo-party）

依赖：仅 Python 标准库（3.8+），无需 pip 安装任何东西。
"""

import argparse
import json
import os
import random
import re
import shutil
import ssl
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("VPN_CONFIG_PATH", os.path.join(SCRIPT_DIR, "vpn_accounts.json"))
PARTY_DIR = os.environ.get("VPN_PARTY_DIR", os.path.expandvars(r"%APPDATA%\mihomo-party"))
UA = "clash-party/1.0.0 (vpn-manager)"

# 部分机场按 User-Agent 返回不同内容（不认识的 UA 只给空模板），逐个尝试
SUB_UAS = [
    "clash-party/1.0.0 (vpn-manager)",
    "clash.meta",
    "mihomo-party/1.0.0",
    "clash-verge/2.0.0",
    "ClashX/1.0",
    "clash",
]

PROXY_TYPE_RE = re.compile(
    r"type:\s*(trojan|ss|ssr|vmess|vless|hysteria2?|tuic|wireguard|http|socks5)\b")


def count_nodes(body_text):
    """统计订阅中的节点数（排除 proxy-groups 的 select/url-test 等）"""
    return len(PROXY_TYPE_RE.findall(body_text))

# 首次运行时的默认配置（已迁移飞鸟云）
DEFAULT_CONFIG = 


def log(msg):
    print(msg, flush=True)


# ============================================================
# 配置读写
# ============================================================
def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def find_airport(cfg, name):
    for ap in cfg["airports"]:
        if ap["name"] == name:
            return ap
    # 支持模糊匹配（唯一前缀）
    hits = [ap for ap in cfg["airports"] if ap["name"].startswith(name)]
    return hits[0] if len(hits) == 1 else None


# ============================================================
# HTTP / 机场交互
# ============================================================
def http_request(url, method="GET", data=None, headers=None, timeout=30):
    req_headers = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    for verify in (True, False):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body
        except ssl.SSLError:
            if verify:
                continue
            return None, {}, b""
        except Exception:
            return None, {}, b""
    return None, {}, b""


def fetch_subscription(url):
    """拉取订阅，返回 (body, headers)；失败 body 为 None。
    自动尝试多种客户端 UA，并拒绝 proxies 为空的模板响应。"""
    headers = {}
    for ua in SUB_UAS:
        status, headers, body = http_request(url, headers={"User-Agent": ua})
        if status != 200 or b"proxies:" not in body:
            continue
        text = body.decode("utf-8", "ignore")
        if re.search(r"(?m)^proxies:\s*\[\s*\]", text) or count_nodes(text) == 0:
            continue  # 空模板，换下一个 UA 再试
        return body, headers
    return None, headers


def login_panel(base, email, password):
    url = base + "/api/v1/passport/auth/login"
    payload = json.dumps({"email": email, "password": password}).encode()
    status, _, body = http_request(url, "POST", payload, {"Content-Type": "application/json"})
    if status == 200:
        try:
            data = json.loads(body).get("data", {})
            return data.get("auth_data") or data.get("token")
        except Exception:
            pass
    return None


def find_subscribe_url(obj):
    if isinstance(obj, str) and "/api/v1/client/subscribe?token=" in obj:
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = find_subscribe_url(v)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = find_subscribe_url(v)
            if r:
                return r
    return None


def get_subscribe_url_from_panel(base, auth):
    for endpoint in ("/api/v1/user/getSubscribe", "/api/v1/user/info"):
        for auth_value in (f"Bearer {auth}", auth):
            status, _, body = http_request(base + endpoint, headers={"Authorization": auth_value})
            if status == 200:
                try:
                    url = find_subscribe_url(json.loads(body))
                    if url:
                        return url
                except Exception:
                    pass
    return None


def resolve_airport(ap, cfg):
    """解析机场订阅，返回 (url, body, headers)；失败返回 (None, None, None)"""
    # 模式 A：直接订阅链接
    if ap.get("subscribe_url"):
        body, headers = fetch_subscription(ap["subscribe_url"])
        if body:
            return ap["subscribe_url"], body, headers
        log(f"    订阅链接直接拉取失败（HTTP 500 多为服务端维护）")

    # 模式 B：账号登录获取新链接
    if ap.get("email") and ap.get("password"):
        bases = ([ap["panel"]] if ap.get("panel") else []) + list(ap.get("mirrors", []))
        for base in bases:
            auth = login_panel(base, ap["email"], ap["password"])
            if not auth:
                continue
            new_url = get_subscribe_url_from_panel(base, auth)
            if not new_url:
                continue
            body, headers = fetch_subscription(new_url)
            if body:
                if new_url != ap.get("subscribe_url"):
                    ap["subscribe_url"] = new_url  # 记住新地址
                    save_config(cfg)
                    log(f"    已获取新订阅链接并保存")
                return new_url, body, headers
    return None, None, None


def parse_userinfo(headers):
    raw = headers.get("subscription-userinfo", "")
    return dict(re.findall(r"(\w+)=(\d+)", raw)) if raw else {}


# ============================================================
# Clash Party 配置操作
# ============================================================
def index_path():
    return os.path.join(PARTY_DIR, "profile.yaml")


def profiles_dir():
    return os.path.join(PARTY_DIR, "profiles")


def read_index():
    with open(index_path(), encoding="utf-8") as f:
        return f.read()


def write_index(text):
    tmp = index_path() + ".tmp"
    try:
        if os.path.exists(index_path()):
            shutil.copy2(index_path(), index_path() + ".bak")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, index_path())
    except OSError as e:
        log(f"    写入 profile.yaml 失败: {e}")
        log("    （Clash Party 可能正在占用该文件，请先退出应用再更新，或在应用内手动更新订阅）")
        raise


def parse_profile_items(text):
    """解析 profile.yaml 的 items 列表，返回 [{id, name, start, end}]
    注意：block 遇到下一个 '  - id:' 行即停止（负向先行断言）"""
    items = []
    for m in re.finditer(
            r"(?m)^  - id: (\S+)[ \t]*\r?\n((?:(?!  - )[ \t]+.*\r?\n)*)", text):
        pid, block = m.group(1), m.group(2)
        nm = re.search(r"(?m)^\s+name:\s*(.+?)\s*$", block)
        name = nm.group(1).strip().strip("'\"") if nm else ""
        items.append({"id": pid, "name": name, "start": m.start(), "end": m.end()})
    return items


def find_profile_id(name):
    if not os.path.exists(index_path()):
        return None
    for it in parse_profile_items(read_index()):
        if it["name"] == name:
            return it["id"]
    return None


def gen_profile_id(text):
    existing = {it["id"] for it in parse_profile_items(text)}
    while True:
        pid = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        if pid not in existing:
            return pid


def _render_block(pid, name, url, updated_ms, extra):
    return (
        f"  - id: {pid}\n"
        f"    name: {name}\n"
        f"    type: remote\n"
        f"    url: {url}\n"
        f"    substore: false\n"
        f"    interval: 1440\n"
        f"    override: []\n"
        f"    useProxy: false\n"
        f"    allowFixedInterval: false\n"
        f"    autoUpdate: true\n"
        f"    updated: {updated_ms}\n"
        f"    extra:\n"
        f"      upload: {extra.get('upload', 0)}\n"
        f"      download: {extra.get('download', 0)}\n"
        f"      total: {extra.get('total', 0)}\n"
        f"      expire: {extra.get('expire', 0)}\n"
    )


def upsert_profile(name, url, updated_ms, extra):
    """更新或新建 Clash Party 订阅配置，返回 profile_id"""
    text = read_index()
    items = parse_profile_items(text)
    pid = next((it["id"] for it in items if it["name"] == name), None)

    if pid:
        # 在原有 block 内打补丁（block 到下一个 '  - id:' 行为止）
        m = re.search(r"(?m)^  - id: " + re.escape(pid) + r"[ \t]*\r?\n(?:(?!  - )[ \t]+.*\r?\n)*",
                      text)
        block = m.group(0)
        new_block = re.sub(r"(?m)^(\s+url:\s*).*$", lambda mm: mm.group(1) + url, block, count=1)
        new_block = re.sub(r"(?m)^(\s+updated:\s*)\d+", lambda mm: mm.group(1) + str(updated_ms),
                           new_block, count=1)
        for key in ("upload", "download", "total", "expire"):
            if key in extra:
                new_block = re.sub(r"(?m)^(\s+" + key + r":\s*)\d+",
                                   lambda mm, v=extra[key]: mm.group(1) + str(v),
                                   new_block, count=1)
        text = text[:m.start()] + new_block + text[m.end():]
    else:
        pid = gen_profile_id(text)
        block = _render_block(pid, name, url, updated_ms, extra)
        if re.search(r"(?m)^current:", text):
            text = re.sub(r"(?m)^current:", block + "current:", text, count=1)
        else:
            text = text.rstrip("\n") + "\n" + block + f"current: {pid}\n"

    write_index(text)
    return pid


def set_current(pid):
    text = read_index()
    if re.search(r"(?m)^current:", text):
        text = re.sub(r"(?m)^current:.*$", f"current: {pid}", text, count=1)
    else:
        text = text.rstrip("\n") + f"\ncurrent: {pid}\n"
    write_index(text)


def write_subscription_file(pid, body):
    os.makedirs(profiles_dir(), exist_ok=True)
    path = os.path.join(profiles_dir(), pid + ".yaml")
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)
    return path


def restart_party():
    log("正在重启 Clash Party ...")
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process 'Clash Party' -ErrorAction SilentlyContinue | "
             "Select-Object -First 1 -ExpandProperty Path"],
            capture_output=True, text=True, timeout=20)
        exe = ps.stdout.strip()
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process 'Clash Party','mihomo' -ErrorAction SilentlyContinue | "
             "Stop-Process -Force"],
            capture_output=True, timeout=20)
        time.sleep(2)
        if exe and os.path.exists(exe):
            subprocess.Popen([exe])
            log(f"    Clash Party 已重启: {exe}")
        else:
            log("    未找到 Clash Party 可执行文件，请手动打开应用")
    except Exception as e:
        log(f"    重启失败: {e}（请手动重启 Clash Party）")


def ask_restart():
    try:
        ans = input("是否重启 Clash Party 使配置立即生效？[y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans == "y":
        restart_party()
    else:
        log("    稍后可在应用内切换订阅或重启应用生效")


# ============================================================
# 命令实现
# ============================================================
def _fmt_gb(n):
    try:
        return f"{int(n) / 1024 ** 3:.2f} GB"
    except Exception:
        return str(n)


def _fmt_expire(ts):
    if not ts or ts == "0":
        return "长期有效"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except Exception:
        return str(ts)


def cmd_list(cfg):
    if not os.path.exists(index_path()):
        log(f"未找到 Clash Party 配置: {index_path()}")
        return
    text = read_index()
    items = {it["name"]: it for it in parse_profile_items(text)}
    current = re.search(r"(?m)^current:\s*(\S+)", text)
    current_id = current.group(1) if current else None

    log(f"账号配置: {CONFIG_PATH}")
    log(f"Clash Party: {PARTY_DIR}")
    log("-" * 72)
    for ap in cfg["airports"]:
        name = ap["name"]
        mode = "订阅链接" if ap.get("subscribe_url") else ("账号登录" if ap.get("email") else "未配置")
        it = items.get(name)
        mark = " <-- 当前使用" if it and it["id"] == current_id else ""
        if it:
            block = text[it["start"]:it["end"]]
            upd = re.search(r"(?m)^\s+updated:\s*(\d+)", block)
            ext = dict(re.findall(r"(?m)^\s+(upload|download|total|expire):\s*(\d+)", block))
            upd_str = time.strftime("%m-%d %H:%M", time.localtime(int(upd.group(1)) / 1000)) if upd else "从未"
            traffic = "-"
            if ext.get("total"):
                left = int(ext["total"]) - int(ext.get("download", 0)) - int(ext.get("upload", 0))
                traffic = f"剩 {_fmt_gb(left)} / 总 {_fmt_gb(ext['total'])}"
            log(f"[{name}] {mode}{mark}")
            log(f"    流量: {traffic} | 到期: {_fmt_expire(ext.get('expire'))} | 本地更新: {upd_str}")
        else:
            log(f"[{name}] {mode} | 尚未写入 Clash Party（请先 update）")
    log("-" * 72)


def _do_update(cfg, ap, restart=None):
    log(f"正在更新 [{ap['name']}] ...")
    url, body, headers = resolve_airport(ap, cfg)
    if not body:
        log(f"    失败：服务端可能仍在维护或凭据失效。请稍后再试。")
        return False

    extra = parse_userinfo(headers)
    updated_ms = int(time.time() * 1000)
    pid = upsert_profile(ap["name"], url, updated_ms, extra)
    ap["profile_id"] = pid
    save_config(cfg)
    path = write_subscription_file(pid, body)

    nodes = count_nodes(body.decode("utf-8", "ignore"))
    log(f"    成功：{nodes} 个节点")
    log(f"    订阅文件: {path}")
    if extra.get("total"):
        left = int(extra["total"]) - int(extra.get("download", 0)) - int(extra.get("upload", 0))
        log(f"    剩余流量: {_fmt_gb(left)} / {_fmt_gb(extra['total'])}，到期: {_fmt_expire(extra.get('expire'))}")

    text = read_index()
    cur = re.search(r"(?m)^current:\s*(\S+)", text)
    if not cur or cur.group(1) != pid:
        log(f"    提示：当前使用的不是 [{ap['name']}]，可执行 switch {ap['name']} 切换")
    if restart:
        restart_party()
    return True


def cmd_update(cfg, name, restart=False):
    if name == "all":
        ok = 0
        for ap in cfg["airports"]:
            if _do_update(cfg, ap, restart):
                ok += 1
        log(f"=== 完成：{ok}/{len(cfg['airports'])} 个机场更新成功 ===")
        return 0 if ok else 1
    ap = find_airport(cfg, name)
    if not ap:
        log(f"未找到机场: {name}（可用 list 查看已配置的机场）")
        return 1
    return 0 if _do_update(cfg, ap, restart) else 1


def cmd_test(cfg, name):
    ap = find_airport(cfg, name)
    if not ap:
        log(f"未找到机场: {name}")
        return 1
    log(f"测试 [{ap['name']}] ...")
    url, body, headers = resolve_airport(ap, cfg)
    if not body:
        log("    不可用（服务端维护或凭据失效）")
        return 1
    nodes = count_nodes(body.decode("utf-8", "ignore"))
    extra = parse_userinfo(headers)
    log(f"    可用：{nodes} 个节点")
    if extra.get("total"):
        left = int(extra["total"]) - int(extra.get("download", 0)) - int(extra.get("upload", 0))
        log(f"    剩余流量: {_fmt_gb(left)} / {_fmt_gb(extra['total'])}，到期: {_fmt_expire(extra.get('expire'))}")
    return 0


def cmd_switch(cfg, name, restart=False):
    ap = find_airport(cfg, name)
    if not ap:
        log(f"未找到机场: {name}")
        return 1
    pid = find_profile_id(ap["name"])
    if not pid:
        log(f"[{ap['name']}] 还没有写入 Clash Party，请先执行: update {ap['name']}")
        return 1
    set_current(pid)
    log(f"已切换当前机场为: {ap['name']} ({pid})")
    if restart:
        restart_party()
    else:
        log("    重启 Clash Party 或在应用内重新选择该订阅后生效（可加 --restart 参数自动重启）")
    return 0


def cmd_remove(cfg, name):
    ap = find_airport(cfg, name)
    if not ap:
        log(f"未找到机场: {name}")
        return 1
    cfg["airports"].remove(ap)
    save_config(cfg)
    log(f"已从管理器移除 [{ap['name']}]（Clash Party 中已生成的配置文件保留）")
    return 0


def cmd_add(cfg):
    log("=== 添加新机场 ===")
    name = input("机场名称（如：扬帆云）: ").strip()
    if not name:
        log("名称不能为空")
        return 1
    if find_airport(cfg, name):
        log(f"已存在同名机场: {name}")
        return 1
    log("配置方式（二选一）：")
    log("  A. 粘贴订阅链接（推荐，机场后台->我的订阅->复制）")
    log("  B. 面板账号密码（自动登录获取链接，链接变了也不用管）")
    url = input("订阅链接（可留空）: ").strip()
    email = password = panel = ""
    if not url:
        email = input("登录邮箱: ").strip()
        password = input("密码: ").strip()
        panel = input("面板地址（如 https://example.com，可留空）: ").strip()
        if not email or not password:
            log("订阅链接和账号密码至少填一种")
            return 1
    ap = {"name": name, "subscribe_url": url or None, "profile_id": None}
    if email:
        ap.update({"email": email, "password": password, "panel": panel or None, "mirrors": []})
    cfg["airports"].append(ap)
    save_config(cfg)
    log(f"已添加 [{name}]")
    ans = input("是否立即更新到 Clash Party？[y/N] ").strip().lower()
    if ans == "y":
        return 0 if _do_update(cfg, ap) else 1
    return 0


def interactive_menu(cfg):
    while True:
        log("")
        log("========== VPN 订阅管理器 ==========")
        for i, ap in enumerate(cfg["airports"], 1):
            pid = find_profile_id(ap["name"])
            log(f"  {i}. {ap['name']}" + ("  [已接入 Clash Party]" if pid else "  [未接入]"))
        log("  a. 添加新机场")
        log("  q. 退出")
        try:
            choice = input("选择要操作的机场（输入序号）: ").strip().lower()
        except EOFError:
            break
        if choice == "q":
            break
        if choice == "a":
            cmd_add(cfg)
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= len(cfg["airports"])):
            log("无效选择")
            continue
        ap = cfg["airports"][int(choice) - 1]
        log("")
        log(f"=== [{ap['name']}] ===")
        log("  1. 更新订阅到 Clash Party")
        log("  2. 测试订阅可用性（不写文件）")
        log("  3. 切换为当前使用的机场")
        log("  4. 返回")
        try:
            act = input("选择操作: ").strip()
        except EOFError:
            continue
        if act == "1":
            _do_update(cfg, ap)
            ask_restart()
        elif act == "2":
            cmd_test(cfg, ap["name"])
        elif act == "3":
            cmd_switch(cfg, ap["name"])
            ask_restart()
        # 4 或其它：返回


# ============================================================
def main():
    parser = argparse.ArgumentParser(description="VPN 订阅管理器 for Clash Party")
    parser.add_argument("--restart", action="store_true", help="更新后重启 Clash Party")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="查看所有机场状态")
    sub.add_parser("add", help="添加新机场（向导）")
    p = sub.add_parser("update", help="更新指定机场或 all")
    p.add_argument("name")
    p = sub.add_parser("switch", help="切换当前使用的机场")
    p.add_argument("name")
    p = sub.add_parser("test", help="测试订阅可用性")
    p.add_argument("name")
    p = sub.add_parser("remove", help="移除机场")
    p.add_argument("name")

    args = parser.parse_args()
    cfg = load_config()

    if args.cmd == "list":
        return cmd_list(cfg)
    if args.cmd == "add":
        return cmd_add(cfg)
    if args.cmd == "update":
        return cmd_update(cfg, args.name, args.restart)
    if args.cmd == "switch":
        return cmd_switch(cfg, args.name, restart=args.restart)
    if args.cmd == "test":
        return cmd_test(cfg, args.name)
    if args.cmd == "remove":
        return cmd_remove(cfg, args.name)

    interactive_menu(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
