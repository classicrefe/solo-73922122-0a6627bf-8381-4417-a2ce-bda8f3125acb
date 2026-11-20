"""单元自测：CSV 解析、盘号登记、退役核对、并行串行化、持久化。
运行：DB_PATH=/tmp/xxx.db python3 tests/test_units.py
"""
import os
import sys
import tempfile
import threading

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DB_PATH"] = _TMP_DB.name

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import service  # noqa: E402
from db import get_conn, init_db  # noqa: E402
from service import ServiceError, parse_manifest_csv, valid_name  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)


def expect_error(name, fn, status=None):
    try:
        fn()
    except ServiceError as e:
        if status is not None:
            assert e.status == status, f"FAILED: {name} 状态码 {e.status} != {status}"
        PASS.append(name)
        return
    raise AssertionError(f"FAILED(应拒绝但未拒绝): {name}")


def h(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


# ---------- 名称规则 ----------
ok("名称:合法", valid_name("WQ-5000_A.1"))
ok("名称:空非法", not valid_name(""))
ok("名称:中文非法", not valid_name("型号一"))
ok("名称:点开头非法", not valid_name(".x"))

# ---------- CSV 解析 ----------
csv1 = "测次编号,摘要\nT1,%s\nT2,%s\n" % (h("a"), h("b"))
p = parse_manifest_csv(csv1)
ok("CSV:带表头解析", len(p.rows) == 2 and p.rows[0] == ("T1", h("a")))

csv2 = "T1,%s\nT1,%s\nT1,%s\n" % (h("a"), h("a"), h("b"))
p = parse_manifest_csv(csv2)
ok("CSV:重复行去重", len(p.rows) == 2 and len(p.duplicates) == 1)
ok("CSV:同测次不同摘要分别保留",
   {d for _, d in p.rows} == {h("a"), h("b")})

csv3 = "T1, ABC\n"
expect_error("CSV:非法摘要拒绝", lambda: parse_manifest_csv(csv3))
expect_error("CSV:三列拒绝", lambda: parse_manifest_csv("T1,%s,x\n" % h("a")))
expect_error("CSV:空内容拒绝", lambda: parse_manifest_csv("  \n"))
expect_error("CSV:空测次拒绝", lambda: parse_manifest_csv(" ,%s\n" % h("a")))
ok("CSV:大写摘要归一", parse_manifest_csv("T1,%s\n" % h("a").upper()).rows[0][1] == h("a"))
ok("CSV:无表头也可解析", len(parse_manifest_csv("T1,%s" % h("a")).rows) == 1)

# ---------- 业务全流程（临时库） ----------
init_db()

with get_conn() as conn:
    mid = service.create_model(conn, "WQ-5000")
    conn.commit()
ok("型号创建", mid > 0)
with get_conn() as conn:
    ok("重复型号返回同一id", service.create_model(conn, "WQ-5000") == mid)
    conn.commit()

# 三块盘：D1、D2 内容相同（互为副本），D3 独有一份
common = "T1,%s\nT2,%s\n" % (h("m1"), h("m2"))
r1 = service.register_disk("WQ-5000", "D1", common)
r2 = service.register_disk("WQ-5000", "D2", common)
r3 = service.register_disk("WQ-5000", "D3", "T3,%s\n" % h("m3"))
ok("登记三块盘", r1["entry_count"] == 2 and r2["entry_count"] == 2 and r3["entry_count"] == 1)

# 已登记盘号不能覆盖
expect_error("盘号不可覆盖", lambda: service.register_disk("WQ-5000", "D1", common), status=409)

# D1 退役：T1/T2 在 D2 有相同测次+摘要副本 → 成功
res = service.retire_disks("WQ-5000", ["D1"])
ok("D1退役成功（D2有副本）", res["ok"] is True)

with get_conn() as conn:
    detail = service.model_detail(conn, "WQ-5000")
st = {d["disk_no"]: d["status"] for d in detail["disks"]}
ok("退役后D1=retired,D2/D3=active",
   st == {"D1": "retired", "D2": "active", "D3": "active"})
ok("退役登记已保留", len(detail["records"]) == 1 and detail["records"][0]["result"] == "retired")

# 已退役盘不能再次退役
expect_error("已退役盘拒绝再退役",
             lambda: service.retire_disks("WQ-5000", ["D1"]), status=409)

# D2+D3 一同退役：T1/T2 范围外无副本（D1 已退役不算），T3 也无 → 整次不生效
res = service.retire_disks("WQ-5000", ["D2", "D3"])
ok("D2+D3退役被拒", res["ok"] is False)
ok("列出全部待迁移测次与摘要",
   sorted((m["test_no"], m["sha256"]) for m in res["missing"])
   == sorted([("T1", h("m1")), ("T2", h("m2")), ("T3", h("m3"))]))
ok("待迁移项标注所在盘",
   sorted(m["on_disk"] for m in res["missing"]) == [["D2"], ["D2"], ["D3"]])
with get_conn() as conn:
    detail = service.model_detail(conn, "WQ-5000")
st = {d["disk_no"]: d["status"] for d in detail["disks"]}
ok("拒绝后盘状态不变", st["D2"] == "active" and st["D3"] == "active")
ok("拒绝登记已保留", detail["records"][0]["result"] == "rejected")

# 同测次不同内容分别核对：D4 上有 T3 但摘要不同 → 不能作为 D3 上 T3 的副本
service.register_disk("WQ-5000", "D4", "T3,%s\n" % h("different"))
res = service.retire_disks("WQ-5000", ["D3"])
ok("同测次不同摘要不算副本", not res["ok"]
   and res["missing"][0]["test_no"] == "T3" and res["missing"][0]["sha256"] == h("m3"))

# 复制到新盘 D5 后补登，再退役 D3 → 成功
service.register_disk("WQ-5000", "D5", "T3,%s\n" % h("m3"))
res = service.retire_disks("WQ-5000", ["D3"])
ok("补登新盘后退役成功", res["ok"] is True)

# 不存在的盘号 / 空选择
expect_error("不存在盘号拒绝", lambda: service.retire_disks("WQ-5000", ["NOPE"]), status=404)
expect_error("空选择拒绝", lambda: service.retire_disks("WQ-5000", []))

# ---------- 并行退役：最后一份副本必须留在在用盘 ----------
# 场景：型号 P 下两块盘 PA、PB 各持唯一副本 X（同测次同摘要），
# 两个线程同时分别退役 PA 和 PB：最多一个成功，另一个必须被拒。
with get_conn() as conn:
    service.create_model(conn, "PMODEL")
    conn.commit()
service.register_disk("PMODEL", "PA", "TX,%s\n" % h("x"))
service.register_disk("PMODEL", "PB", "TX,%s\n" % h("x"))
barrier = threading.Barrier(2)
results = {}


def retire_a():
    barrier.wait()
    try:
        results["A"] = service.retire_disks("PMODEL", ["PA"])["ok"]
    except ServiceError as e:
        results["A"] = f"ERR {e}"


def retire_b():
    barrier.wait()
    try:
        results["B"] = service.retire_disks("PMODEL", ["PB"])["ok"]
    except ServiceError as e:
        results["B"] = f"ERR {e}"


t1 = threading.Thread(target=retire_a)
t2 = threading.Thread(target=retire_b)
t1.start(); t2.start(); t1.join(); t2.join()
ok("并行退役恰好一方成功", sorted(results.values(), key=str) == [False, True], str(results))
with get_conn() as conn:
    detail = service.model_detail(conn, "PMODEL")
active = [d["disk_no"] for d in detail["disks"] if d["status"] == "active"]
ok("最后一份副本留在在用盘", len(active) == 1 and active[0] in ("PA", "PB"))

# 反向场景：三块盘同持 X，并行退役其中两块 → 都应成功（外面还剩一块）
with get_conn() as conn:
    service.create_model(conn, "QMODEL")
    conn.commit()
for dn in ("QA", "QB", "QC"):
    service.register_disk("QMODEL", dn, "TX,%s\n" % h("x"))
barrier = threading.Barrier(2)
results2 = {}


def retire_q(letter, store):
    barrier.wait()
    try:
        results2[store] = service.retire_disks("QMODEL", [f"Q{letter}"])["ok"]
    except ServiceError:
        results2[store] = False


t1 = threading.Thread(target=retire_q, args=("A", "A"))
t2 = threading.Thread(target=retire_q, args=("B", "B"))
t1.start(); t2.start(); t1.join(); t2.join()
ok("并行退役两块且外部仍有副本:均成功", results2 == {"A": True, "B": True}, str(results2))

# ---------- 持久化：重新初始化（模拟重启）后数据仍在 ----------
init_db()  # CREATE TABLE IF NOT EXISTS，不应破坏数据
with get_conn() as conn:
    detail = service.model_detail(conn, "WQ-5000")
ok("重启后盘片与退役登记可查",
   len(detail["disks"]) == 5 and len(detail["records"]) >= 3)

print(f"全部 {len(PASS)} 项单元自测通过")
