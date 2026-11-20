"""集成自测：对运行中的服务执行关键操作（默认 http://127.0.0.1:8000）。
运行：BASE_URL=http://127.0.0.1:8000 python3 tests/selftest.py
覆盖：型号/盘片登记 API、CSV 校验、盘号不可覆盖、单盘与多盘退役、
缺副本整次不生效并列待迁移清单、同测次不同摘要分别核对、并行退役、
退役登记查询、HTML 页面冒烟、重启后可查（由测试库文件持久化保证）。
"""
import hashlib
import os
import threading
import uuid

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)
    print(f"  ✔ {name}")


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.get("/api/health")
    ok("健康检查", r.status_code == 200 and r.json()["status"] == "ok")

    model = f"m{uuid.uuid4().hex[:10]}"
    r = c.post("/api/models", json={"name": model})
    ok("创建型号", r.status_code == 201, r.text)
    r = c.post("/api/models", json={"name": model})
    ok("重复型号不报错(幂等)", r.status_code == 201)

    # ---- 盘片登记 ----
    print("盘片登记")
    d1_csv = "测次编号,摘要\nT1,%s\nT2,%s\n" % (h("m1"), h("m2"))
    r = c.post(f"/api/models/{model}/disks",
               json={"disk_no": "D1", "csv": d1_csv})
    ok("D1登记2份", r.status_code == 201 and r.json()["entry_count"] == 2, r.text)
    r = c.post(f"/api/models/{model}/disks",
               json={"disk_no": "D2", "csv": d1_csv})
    ok("D2登记2份(副本)", r.status_code == 201, r.text)
    r = c.post(f"/api/models/{model}/disks",
               json={"disk_no": "D3", "csv": "T3,%s" % h("m3")})
    ok("D3登记1份(独有)", r.status_code == 201, r.text)

    r = c.post(f"/api/models/{model}/disks",
               json={"disk_no": "D1", "csv": d1_csv})
    ok("已登记盘号不可覆盖(409)", r.status_code == 409 and "不能覆盖" in r.json()["message"], r.text)

    r = c.post(f"/api/models/{model}/disks",
               json={"disk_no": "BAD", "csv": "T1,xyz\n"})
    ok("非法摘要被拒(422)", r.status_code == 422, r.text)
    r = c.post(f"/api/models/{model}/disks",
               json={"disk_no": "BAD", "csv": "T1,%s,extra\n" % h("x")})
    ok("列数错误被拒(422)", r.status_code == 422, r.text)

    st = c.get(f"/api/models/{model}").json()
    ok("型号状态含3块在用盘",
       len(st["disks"]) == 3 and all(d["status"] == "active" for d in st["disks"]))
    entries = c.get(f"/api/models/{model}/disks/D1/entries").json()["entries"]
    ok("清单明细可查", len(entries) == 2 and entries[0]["test_no"] == "T1")

    # ---- 单盘退役成功 ----
    print("退役流程")
    r = c.post(f"/api/models/{model}/retire", json={"disk_nos": ["D1"]})
    ok("D1退役成功(D2有副本)", r.status_code == 200 and r.json()["ok"] is True, r.text)
    st = c.get(f"/api/models/{model}").json()
    status = {d["disk_no"]: d["status"] for d in st["disks"]}
    ok("退役状态落库", status["D1"] == "retired" and status["D2"] == "active")

    r = c.post(f"/api/models/{model}/retire", json={"disk_nos": ["D1"]})
    ok("已退役盘再退役被拒(409)", r.status_code == 409, r.text)

    # ---- 多盘一同退役，缺副本整次不生效 ----
    r = c.post(f"/api/models/{model}/retire", json={"disk_nos": ["D2", "D3"]})
    body = r.json()
    ok("D2+D3退役被拒(409)", r.status_code == 409 and body["ok"] is False, r.text)
    missing = {(m["test_no"], m["sha256"]) for m in body["missing"]}
    ok("列出全部待迁移测次与摘要",
       missing == {("T1", h("m1")), ("T2", h("m2")), ("T3", h("m3"))}, str(missing))
    ok("待迁移项标注所在盘",
       all(m["on_disk"] for m in body["missing"]))
    st = c.get(f"/api/models/{model}").json()
    status = {d["disk_no"]: d["status"] for d in st["disks"]}
    ok("整次不生效:盘状态均未变", status["D2"] == "active" and status["D3"] == "active")

    # ---- 同测次不同摘要分别核对 ----
    r = c.post(f"/api/models/{model}/disks",
               json={"disk_no": "D4", "csv": "T3,%s" % h("different")})
    assert r.status_code == 201, r.text
    r = c.post(f"/api/models/{model}/retire", json={"disk_nos": ["D3"]})
    ok("同测次不同摘要不算副本", r.status_code == 409
       and r.json()["missing"][0]["sha256"] == h("m3"), r.text)

    # 复制到新盘补登后退役成功
    r = c.post(f"/api/models/{model}/disks",
               json={"disk_no": "D5", "csv": "T3,%s" % h("m3")})
    assert r.status_code == 201, r.text
    r = c.post(f"/api/models/{model}/retire", json={"disk_nos": ["D3"]})
    ok("补登新盘后D3退役成功", r.status_code == 200 and r.json()["ok"] is True, r.text)

    # ---- 退役登记保留、可查 ----
    recs = c.get(f"/api/models/{model}/retirements").json()["retirements"]
    ok("退役登记含成功与拒绝",
       {x["result"] for x in recs} == {"retired", "rejected"} and len(recs) >= 4)
    rejected = [x for x in recs if x["result"] == "rejected"]
    ok("拒绝登记带缺副本明细", all(x["missing"] for x in rejected))

    # ---- 并行退役：最后一份副本留在在用盘 ----
    print("并行退役")
    pm = f"p{uuid.uuid4().hex[:10]}"
    assert c.post("/api/models", json={"name": pm}).status_code == 201
    for dn in ("PA", "PB"):
        assert c.post(f"/api/models/{pm}/disks",
                      json={"disk_no": dn, "csv": "TX,%s" % h("x")}).status_code == 201
    barrier = threading.Barrier(2)
    outcomes = {}

    def call_retire(disk, key):
        barrier.wait()
        r = c.post(f"/api/models/{pm}/retire", json={"disk_nos": [disk]})
        outcomes[key] = r.status_code == 200 and r.json()["ok"]

    t1 = threading.Thread(target=call_retire, args=("PA", "A"))
    t2 = threading.Thread(target=call_retire, args=("PB", "B"))
    t1.start(); t2.start(); t1.join(); t2.join()
    ok("并行退役恰好一方成功", sorted(outcomes.values()) == [False, True], str(outcomes))
    st = c.get(f"/api/models/{pm}").json()
    active = [d["disk_no"] for d in st["disks"] if d["status"] == "active"]
    ok("最后一份副本留在在用盘", len(active) == 1, str(active))

    # 三块盘同持一份报文，并行退役两块 → 都成功
    qm = f"q{uuid.uuid4().hex[:10]}"
    assert c.post("/api/models", json={"name": qm}).status_code == 201
    for dn in ("QA", "QB", "QC"):
        assert c.post(f"/api/models/{qm}/disks",
                      json={"disk_no": dn, "csv": "TX,%s" % h("x")}).status_code == 201
    barrier = threading.Barrier(2)
    outcomes2 = {}

    def retire_qa():
        barrier.wait()
        r = c.post(f"/api/models/{qm}/retire", json={"disk_nos": ["QA"]})
        outcomes2["A"] = r.json()["ok"]

    def retire_qb():
        barrier.wait()
        r = c.post(f"/api/models/{qm}/retire", json={"disk_nos": ["QB"]})
        outcomes2["B"] = r.json()["ok"]

    t1 = threading.Thread(target=retire_qa)
    t2 = threading.Thread(target=retire_qb)
    t1.start(); t2.start(); t1.join(); t2.join()
    ok("外部仍有副本时并行退役均成功",
       outcomes2 == {"A": True, "B": True}, str(outcomes2))

    # ---- HTML 页面冒烟 ----
    print("HTML 页面")
    r = c.get("/")
    ok("首页可打开", r.status_code == 200 and "设备型号" in r.text)
    r = c.get(f"/models/{model}")
    ok("型号页可打开", r.status_code == 200
       and "按盘号登记 CSV 清单" in r.text and "退役登记" in r.text)
    r = c.post(f"/models/{model}/disks",
               data={"disk_no": "WEB1", "csv_text": "TW,%s" % h("w"), "note": "网页登记"})
    ok("网页表单登记成功(303)", r.status_code == 303 and "/models/" in r.headers["location"])
    # 网页退役一块缺副本的盘：应 303 跳回且登记为 rejected
    r = c.post(f"/models/{model}/retire", data={"disk_nos": ["WEB1"]})
    ok("网页退役缺副本被拦(303回跳)", r.status_code == 303)
    recs = c.get(f"/api/models/{model}/retirements").json()["retirements"]
    web_reject = [x for x in recs if x["result"] == "rejected" and "WEB1" in x["disk_nos"]]
    ok("网页拒绝已留登记", bool(web_reject))
    r = c.get(f"/models/{model}?notice=%7B%22ok%22%3Atrue%2C%22message%22%3A%22x%22%7D")
    ok("带通知参数页面可渲染", r.status_code == 200 and "操作成功" in r.text)

    print(f"\n全部 {len(PASS)} 项集成自测通过")
    return model


if __name__ == "__main__":
    used = main()
    print(f"MODEL={used}")
