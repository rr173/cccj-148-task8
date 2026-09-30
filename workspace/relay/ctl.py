"""操作台 CLI：

  python3 -m relay.ctl board  --url URL                 四栏总览
  python3 -m relay.ctl map    --url URL --voyage VID    扫描→当前航次映射
  python3 -m relay.ctl ignore|remap|reconfirm ...       处置（可带 --request-id）
  python3 -m relay.ctl events --url URL
  python3 -m relay.ctl verify --url URL
  python3 -m relay.ctl set-voyage --url URL --voyage VID
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

import urllib.request
import urllib.error


def call(method, url, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None \
        else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _base(url):
    return url.rstrip("/")


def show_board(base, spool_root=None):
    _, sess = call("GET", f"{base}/relay/sessions")
    _, pend = call("GET", f"{base}/relay/pending")
    _, gap = call("GET", f"{base}/relay/gaps")
    _, quar = call("GET", f"{base}/relay/quarantine")
    _, app = call("GET", f"{base}/relay/applied")
    _, rej = call("GET", f"{base}/relay/rejected")

    print("=" * 78)
    print("【待传】终端侧已封段、尚未被确认（pending/sent）")
    print("=" * 78)
    if spool_root:
        import glob
        from relay.segment import DeviceSpool
        for state_file in sorted(glob.glob(f"{spool_root}/*/state.json")):
            device = os.path.basename(os.path.dirname(state_file))
            sp = DeviceSpool(spool_root, device)
            waiting = [s for s in sp.segments()
                       if s["status"] in ("pending", "sent")]
            if waiting:
                print(f"  {device}（会话 {sp.session()}）待传序号: "
                      + ", ".join(f"#{s['seq']}({s['status']})" for s in waiting))
            else:
                print(f"  {device}: 无待传段（全部 acked）")
    else:
        print("  （用 --spool <spool根目录> 可查看终端侧待传段）")

    print("\n" + "=" * 78)
    print("会话与游标（接收侧收件进度）")
    print("=" * 78)
    for s in sess.get("sessions", []):
        print(f"  {s['session']}  游标={s['cursor']} 下一期望#{s['next_expected']}"
              f"  已应用={s['applied_count']} 待处理={s['pending_count']}")
        for g in s["staged"]:
            print(f"      [断层暂存] #{g['seq']}  {g['message']}")
        for q in s["quarantine"]:
            print(f"      [隔离]     #{q['seq']}  {q['message']}")

    def section(title, rows, fmt):
        print("\n" + "=" * 78); print(title); print("=" * 78)
        if not rows:
            print("  （空）")
        for x in rows:
            print("  " + fmt(x))

    section("【待处理 pending】等待操作员忽略 / 重新映射 / 重调确认",
            pend.get("pending", []),
            lambda r: (f"{r['record_id'][:20]}  {r['container']} "
                      f"上报槽位={r['slot']}  原因={r['reason']}  "
                      f"设备={r['device_id']}"))
    section("【断层 gaps】已暂存但不能推进游标",
            gap.get("gaps", []),
            lambda r: (f"{r['session']} 游标={r['cursor']} 缺={r['missing']} "
                      f"暂存序号={[s['seq'] for s in r['staged']]}"))
    section("【隔离 quarantine】同序号不同内容",
            quar.get("quarantine", []),
            lambda r: f"{r['session']} #{r['seq']}  {r['message']}")
    section(f"【已应用 applied】（另有 {app.get('ignored_count', 0)} 条被忽略）",
            app.get("applied", []),
            lambda r: (f"{r['container']} 槽位={r['resolved_slot'] or r['slot']}"
                      f" 确认={r['confirmation_id']} 记录={r['record_id'][:16]}"))
    section("【整包拒绝 rejected】校验和错 / 记录非法（未写入任何记录）",
            rej.get("rejected", []),
            lambda r: f"{r['session']} #{r['seq']} {r['error_code']}: {r['message']}")


def show_map(base, voyage):
    _, d = call("GET", f"{base}/relay/voyages/{voyage}/map")
    plan = d.get("current_plan")
    print(f"航次 {voyage}  当前方案：{plan}")
    print(f"{'箱号':14} {'上报槽位':10} {'方案槽位':10} {'匹配':4} {'状态':8} "
          f"{'原因':22} 设备/序号")
    for r in d.get("records", []):
        print(f"{r['container']:14} {r['scanned_slot']:10} "
              f"{str(r['current_plan_slot']):10} "
              f"{'是' if r['slot_matches_plan'] else '否':4} "
              f"{r['status']:8} {str(r['reason']):22} "
              f"{r['device_id']} #{r['seq']}")


def do_dispose(base, args):
    body = {"request_id": args.request_id or f"disp_{uuid.uuid4().hex[:12]}",
            "record_id": args.record_id, "action": args.action,
            "operator_id": args.operator, "note": args.note}
    if args.new_slot:
        body["new_slot"] = args.new_slot
    status, d = call("POST", f"{base}/relay/dispose", body)
    print(f"HTTP {status}")
    print(json.dumps(d, ensure_ascii=False, indent=2))
    print(f"request_id={body['request_id']}（同一请求重发将回放此结果）")
    return body["request_id"]


def main(argv=None):
    p = argparse.ArgumentParser(description="中继器操作台")
    p.add_argument("--url", default="http://127.0.0.1:8090")
    sub = p.add_subparsers(dest="cmd", required=True)

    sb = sub.add_parser("board")
    sb.add_argument("--spool", help="终端 spool 根目录，用于显示待传段")
    m = sub.add_parser("map"); m.add_argument("--voyage", required=True)
    sub.add_parser("events")
    sub.add_parser("verify")
    sv = sub.add_parser("set-voyage"); sv.add_argument("--voyage", required=True)

    for name in ("ignore", "remap", "reconfirm"):
        s = sub.add_parser(name)
        s.add_argument("record_id")
        s.add_argument("--new-slot")
        s.add_argument("--request-id")
        s.add_argument("--operator", default="operator")
        s.add_argument("--note")
        s.set_defaults(action=name)

    args = p.parse_args(argv)
    base = _base(args.url)
    if args.cmd == "board":
        show_board(base, spool_root=args.spool)
    elif args.cmd == "map":
        show_map(base, args.voyage)
    elif args.cmd == "events":
        _, d = call("GET", f"{base}/relay/events")
        for e in d.get("events", []):
            print(f"#{e['id']} {e['created_at']} {e['session']} #{e['seq']} "
                  f"{e['kind']} {json.dumps(e['detail'], ensure_ascii=False) if e['detail'] else ''}")
    elif args.cmd == "verify":
        _, d = call("GET", f"{base}/relay/verify")
        print(json.dumps(d, ensure_ascii=False, indent=2))
        sys.exit(0 if d.get("ok") else 2)
    elif args.cmd == "set-voyage":
        st, d = call("POST", f"{base}/relay/config",
                     {"active_voyage": args.voyage})
        print(st, d)
    else:
        do_dispose(base, args)


if __name__ == "__main__":
    main()
