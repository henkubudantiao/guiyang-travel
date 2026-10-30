#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
旅行 AA 记账 — 零依赖双栈（IPv6+IPv4）web 服务  [v2 多行程版]
规则引擎全在服务端：金额一律以「分」为整数计算，结算方案精确到分。
结构：state = {trips: [{id,name,startDate,endDate,people,expenses}], activeTripId}
旧版 v1（单 tripName/people/expenses）加载时自动迁移为第一条行程，原文件留档。
前端 POST /api/cmd {type,...}，GET /api/state 返回 state + summary（当前行程）。
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
LOCK = threading.Lock()

CATEGORIES = {
    "food": "餐饮", "hotel": "住宿", "traffic": "交通",
    "ticket": "门票", "shopping": "购物", "fun": "娱乐", "other": "其他",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERSON_COLORS = ["#4f8cff", "#ff7043", "#2ec4a6", "#f0a500", "#a06cd5", "#e05c7e", "#3aa6dd", "#7cb342"]
MAX_PEOPLE = 8
MAX_TRIPS = 30

_id_counter = [0]


def new_id():
    _id_counter[0] += 1
    return "%d-%04x%02x" % (int(time.time() * 1000), os.getpid() & 0xFFFF, _id_counter[0] & 0xFF)


def new_trip(name, created=None):
    return {
        "id": new_id(), "name": name,
        "startDate": "", "endDate": "",
        "createdAt": int(created or time.time() * 1000),
        "people": [], "expenses": [],
    }


def trip_by_id(st, tid):
    for t in st["trips"]:
        if t["id"] == tid:
            return t
    return None


def current_trip(st):
    t = trip_by_id(st, st.get("activeTripId"))
    return t if t else st["trips"][0]


def default_state():
    t = new_trip("第一段旅程")
    return {"version": 2, "trips": [t], "activeTripId": t["id"], "updatedAt": 0}


def migrate(st):
    """v1 -> v2：原字段打包成第一条行程。已是 v2 则仅做兜底修复。"""
    if st.get("version") == 2 and isinstance(st.get("trips"), list) and st["trips"]:
        if not st.get("activeTripId") or not trip_by_id(st, st["activeTripId"]):
            st["activeTripId"] = st["trips"][0]["id"]
        return st
    t = new_trip((st.get("tripName") or "旅行账本")[:30], created=st.get("updatedAt") or None)
    t["people"] = st.get("people") or []
    t["expenses"] = st.get("expenses") or []
    return {"version": 2, "trips": [t], "activeTripId": t["id"], "updatedAt": 0}


def load_state():
    if not os.path.exists(DATA_FILE):
        return default_state()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        was_v1 = st.get("version") != 2
        st = migrate(st)
        if was_v1:
            try:
                shutil.copy(DATA_FILE, DATA_FILE + ".v1-archived-%d" % int(time.time()))
                print("[%s] v1 数据已自动迁移为多行程结构，原文件留档" % time.strftime("%H:%M:%S"), flush=True)
            except Exception:
                pass
            save_state(st)
        return st
    except Exception:
        try:
            shutil.copy(DATA_FILE, DATA_FILE + ".corrupt-%d" % int(time.time()))
        except Exception:
            pass
        return default_state()


def save_state(st):
    st["updatedAt"] = int(time.time() * 1000)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)


STATE = load_state()


def parse_amount_fen(raw):
    """把 '123.4' / 123.4 / '12345' 统一转成整数的分；非法返回 None。最多两位小数，>0，<1亿元。"""
    s = str(raw).strip().replace("￥", "").replace("¥", "").replace(",", "").replace("，", "")
    if not re.match(r"^\d+(\.\d{1,2})?$", s):
        return None
    if "." in s:
        a, b = s.split(".")
        fen = int(a) * 100 + int((b + "00")[:2])
    else:
        fen = int(s) * 100
    if fen <= 0 or fen > 10 ** 10:
        return None
    return fen


def person_by_id(trip, pid):
    for p in trip["people"]:
        if p["id"] == pid:
            return p
    return None


def expense_by_id(trip, eid):
    for e in trip["expenses"]:
        if e["id"] == eid:
            return e
    return None


def today_str():
    return time.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 结算引擎（分单位精确，规则只写这一处）
def compute_summary(trip):
    people = trip["people"]
    n = len(people)
    total = sum(e["fen"] for e in trip["expenses"])
    paid = {p["id"]: 0 for p in people}
    for e in trip["expenses"]:
        if e["payerId"] in paid:
            paid[e["payerId"]] += e["fen"]
    # 人均份额：向下取整，余数（分）按「垫付多者优先」逐人 +1 分，保证 Σ份额 == 总额
    base = total // n if n else 0
    rem = total - base * n if n else 0
    order = sorted(people, key=lambda p: -paid[p["id"]])
    share = {p["id"]: base for p in people}
    for p in order[:rem]:
        share[p["id"]] += 1
    # 余额 = 已垫付 - 应分摊；>0 应收回，<0 应支出
    balance = {p["id"]: paid[p["id"]] - share[p["id"]] for p in people}
    # 最简转账方案（贪心精确匹配，单位分，总和为零 → 无误差）
    debtors = sorted([{"id": p["id"], "amt": -balance[p["id"]]} for p in people if balance[p["id"]] < 0],
                     key=lambda x: -x["amt"])
    creditors = sorted([{"id": p["id"], "amt": balance[p["id"]]} for p in people if balance[p["id"]] > 0],
                       key=lambda x: -x["amt"])
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amt = min(debtors[i]["amt"], creditors[j]["amt"])
        if amt > 0:
            transfers.append({"from": debtors[i]["id"], "to": creditors[j]["id"], "fen": amt})
        debtors[i]["amt"] -= amt
        creditors[j]["amt"] -= amt
        if debtors[i]["amt"] == 0:
            i += 1
        if creditors[j]["amt"] == 0:
            j += 1
    by_cat = {}
    for e in trip["expenses"]:
        by_cat[e.get("cat", "other")] = by_cat.get(e.get("cat", "other"), 0) + e["fen"]
    return {
        "totalFen": total,
        "count": len(trip["expenses"]),
        "perPersonFen": share,
        "paidFen": paid,
        "balanceFen": balance,
        "transfers": transfers,
        "byCat": by_cat,
    }


# ---------------------------------------------------------------- 命令
def apply_cmd(st, c):
    """返回 (ok, msg)；所有业务校验在这里。行程命令作用于全库，其余只作用于「当前行程」。"""
    t = c.get("type")

    # ---------- 行程管理 ----------
    if t == "add_trip":
        name = (c.get("name") or "").strip()[:30]
        if not name:
            return False, "行程名称不能为空"
        if len(st["trips"]) >= MAX_TRIPS:
            return False, "最多 %d 段行程" % MAX_TRIPS
        trip = new_trip(name)
        if DATE_RE.match(str(c.get("startDate") or "").strip()):
            trip["startDate"] = str(c["startDate"]).strip()
        if DATE_RE.match(str(c.get("endDate") or "").strip()):
            trip["endDate"] = str(c["endDate"]).strip()
        st["trips"].append(trip)
        st["activeTripId"] = trip["id"]          # 新建即切入，直接开始记
        return True, "已新建并切换到行程「%s」" % name

    if t == "switch_trip":
        trip = trip_by_id(st, c.get("id"))
        if not trip:
            return False, "行程不存在"
        st["activeTripId"] = trip["id"]
        return True, "已切换到「%s」" % trip["name"]

    if t == "rename_trip":
        trip = trip_by_id(st, c.get("id")) if c.get("id") else current_trip(st)
        if not trip:
            return False, "行程不存在"
        name = (c.get("name") or "").strip()[:30]
        if not name:
            return False, "行程名称不能为空"
        trip["name"] = name
        if c.get("startDate") is not None:
            trip["startDate"] = c["startDate"] if DATE_RE.match(str(c.get("startDate") or "").strip()) else ""
        if c.get("endDate") is not None:
            trip["endDate"] = c["endDate"] if DATE_RE.match(str(c.get("endDate") or "").strip()) else ""
        return True, "已更新行程信息"

    if t == "delete_trip":
        trip = trip_by_id(st, c.get("id"))
        if not trip:
            return False, "行程不存在"
        if len(st["trips"]) <= 1:
            return False, "至少保留一段行程"
        st["trips"] = [q for q in st["trips"] if q["id"] != trip["id"]]
        if st["activeTripId"] == trip["id"]:
            st["activeTripId"] = st["trips"][-1]["id"]
        return True, "已删除行程「%s」及其全部记录" % trip["name"]

    # ---------- 当前行程内的记账 ----------
    trip = current_trip(st)

    if t == "set_trip":                       # 兼容旧命令名：改当前行程名
        return apply_cmd(st, {"type": "rename_trip", "name": c.get("name")})

    if t == "add_person":
        name = (c.get("name") or "").strip()[:12]
        if not name:
            return False, "姓名不能为空"
        if len(trip["people"]) >= MAX_PEOPLE:
            return False, "最多 %d 位同行者" % MAX_PEOPLE
        if any(p["name"] == name for p in trip["people"]):
            return False, "本行程已有同名旅伴「%s」" % name
        trip["people"].append({
            "id": new_id(), "name": name,
            "color": PERSON_COLORS[len(trip["people"]) % len(PERSON_COLORS)],
            "joinedAt": int(time.time() * 1000),
        })
        return True, "已在「%s」添加旅伴「%s」" % (trip["name"], name)

    if t == "remove_person":
        p = person_by_id(trip, c.get("id"))
        if not p:
            return False, "旅伴不存在"
        used = sum(1 for e in trip["expenses"] if e["payerId"] == p["id"])
        if used:
            return False, "「%s」还有 %d 笔垫付记录，请先删除相关消费" % (p["name"], used)
        trip["people"] = [q for q in trip["people"] if q["id"] != p["id"]]
        return True, "已移除「%s」，支出将重新平分" % p["name"]

    if t == "add_expense":
        if not trip["people"]:
            return False, "请先在当前行程添加旅伴"
        item = (c.get("item") or "").strip()[:60]
        if not item:
            return False, "消费事项不能为空"
        fen = parse_amount_fen(c.get("amount"))
        if fen is None:
            return False, "金额格式不对，请输入正数（最多两位小数）"
        p = person_by_id(trip, c.get("payerId"))
        if not p:
            return False, "请选择付款人"
        cat = c.get("cat") if c.get("cat") in CATEGORIES else "other"
        date = (c.get("date") or "").strip()
        if not DATE_RE.match(date):
            date = today_str()
        trip["expenses"].append({
            "id": new_id(), "item": item, "fen": fen, "payerId": p["id"],
            "cat": cat, "date": date, "ts": int(time.time() * 1000),
            "perPersonFen": fen // len(trip["people"]),
        })
        return True, "已记入「%s」：%s ¥%.2f（%s垫付）" % (trip["name"], item, fen / 100, p["name"])

    if t == "edit_expense":
        e = expense_by_id(trip, c.get("id"))
        if not e:
            return False, "记录不存在（只能修改当前行程的记录）"
        if c.get("item") is not None:
            item = str(c["item"]).strip()[:60]
            if not item:
                return False, "消费事项不能为空"
            e["item"] = item
        if c.get("amount") is not None:
            fen = parse_amount_fen(c["amount"])
            if fen is None:
                return False, "金额格式不对"
            e["fen"] = fen
        if c.get("payerId") is not None:
            p = person_by_id(trip, c["payerId"])
            if not p:
                return False, "付款人不在当前行程"
            e["payerId"] = p["id"]
        if c.get("cat") in CATEGORIES:
            e["cat"] = c["cat"]
        if c.get("date") and DATE_RE.match(str(c["date"]).strip()):
            e["date"] = str(c["date"]).strip()
        e["perPersonFen"] = e["fen"] // max(len(trip["people"]), 1)
        return True, "已修改记录"

    if t == "delete_expense":
        e = expense_by_id(trip, c.get("id"))
        if not e:
            return False, "记录不存在（只能删除当前行程的记录）"
        trip["expenses"] = [q for q in trip["expenses"] if q["id"] != e["id"]]
        return True, "已删除「%s」" % e["item"]

    if t == "clear_expenses":
        trip["expenses"] = []
        return True, "已清空行程「%s」的全部消费" % trip["name"]

    return False, "未知命令: %s" % t


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "TravelSplit/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        code = str(args[1]) if len(args) > 1 else ""
        if code.startswith(("4", "5")):
            print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args), flush=True)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            with LOCK:
                trip = current_trip(STATE)
                return self._json({"ok": True, "state": STATE,
                                   "tripId": trip["id"], "summary": compute_summary(trip)})
        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC_DIR, "index.html"))
        return self._file(os.path.join(STATIC_DIR, path.lstrip("/")))

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/cmd":
            return self._json({"ok": False, "msg": "not found"}, 404)
        c = self._body()
        with LOCK:
            ok, msg = apply_cmd(STATE, c)
            if ok:
                save_state(STATE)
            trip = current_trip(STATE)
        self._json({"ok": ok, "msg": msg, "state": STATE,
                    "tripId": trip["id"], "summary": compute_summary(trip)})

    def _file(self, p):
        p = os.path.normpath(p)
        if not p.startswith(STATIC_DIR) or not os.path.isfile(p):
            return self._send(404, {"ok": False, "msg": "not found"})
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".png": "image/png"}.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype)


class V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        if hasattr(socket, "IPV6_V6ONLY"):
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        super().server_bind()


def access_urls(port):
    urls = []
    try:
        out = subprocess.run(["ip", "-6", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                addr = parts[3].split("/")[0]
                stable = "mngtmpaddr" in line or "ff:fe" in addr
                urls.append("http://[%s]:%d/  %s" % (addr, port, "(稳定,EUI-64)" if stable else ""))
    except Exception:
        pass
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            ip = parts[3].split("/")[0]
            try:
                a = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if a in ipaddress.ip_network("172.16.0.0/12") or a.is_loopback or a.is_link_local:
                continue
            urls.append("http://%s:%d/  (局域网)" % (ip, port))
    except Exception:
        pass
    return urls


def main():
    ap = argparse.ArgumentParser(description="旅行 AA 记账服务")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="::")
    args = ap.parse_args()

    srv = V6Server((args.host, args.port), Handler)
    print("服务已启动，监听 [%s]:%d" % (args.host, args.port), flush=True)
    for u in access_urls(args.port):
        print("  访问地址: %s" % u, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with LOCK:
            save_state(STATE)
        srv.server_close()


if __name__ == "__main__":
    main()
