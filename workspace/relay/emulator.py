"""边缘模拟器：码头手持终端 + 中继上传器。

用法：
  python3 -m relay.emulator init <spool> <device_id>
  python3 -m relay.emulator scan <spool> <device_id> <箱号> <槽位> [--at TS]
  python3 -m relay.emulator seal <spool> <device_id> [--voyage VID]
  python3 -m relay.emulator sessions <spool> <device_id>
  python3 -m relay.emulator list <spool> <device_id>
  python3 -m relay.emulator upload <spool> <device_id> --url URL [--all]
  python3 -m relay.emulator send <spool> <device_id> <seq> --url URL
  python3 -m relay.emulator tamper <spool> <device_id> <seq> --url URL
        # 改一条箱号但不重算校验和 => 校验和错误
  python3 -m relay.emulator bad-record <spool> <device_id> <seq> --url URL
        # 改一条箱号并重算校验和 => 包内记录非法
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

from . import protocol
from .segment import DeviceSpool


def http_post(url: str, body_text: str) -> tuple[int, dict, str]:
    req = urllib.request.Request(
        url, data=body_text.encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8")
        try:
            return e.code, json.loads(text), text
        except ValueError:
            return e.code, {}, text
    except urllib.error.URLError as e:
        return 0, {}, str(e)


def send_packet(spool: DeviceSpool, seq: int, url: str) -> dict:
    env = spool.read_packet(seq)
    return _send(spool, seq, json.dumps(env, ensure_ascii=False), url)


def _send(spool, seq, text, url):
    status, body, raw = http_post(url, text)
    if status == 0:
        return {"seq": seq, "http": 0, "error": raw}
    spool.mark_sent(seq)
    ack = body.get("ack", body.get("error", body))
    if status == 200:
        spool.mark_acked(seq, ack)
    return {"seq": seq, "http": status, "ack": ack}


def upload_next(spool: DeviceSpool, url: str) -> dict | None:
    seq = spool.next_seq()
    if seq is None:
        return None
    return send_packet(spool, seq, url)


def cmd_upload(spool, args):
    url = args.url.rstrip("/") + "/relay/upload"
    results = []
    while True:
        r = upload_next(spool, url)
        if r is None:
            break
        results.append(r)
        if not args.all:
            break
        # 断层/隔离/拒绝都不要继续盲发后面的序号
        ack = r.get("ack") or {}
        if r["http"] != 200 or ack.get("status") in ("staged", "quarantine"):
            break
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return results


def cmd_send(spool, args):
    url = args.url.rstrip("/") + "/relay/upload"
    print(json.dumps(send_packet(spool, args.seq, url), ensure_ascii=False,
                     indent=2))


def cmd_tamper(spool, args):
    """改内容不重算 checksum => 校验和错误，整包拒绝。"""
    url = args.url.rstrip("/") + "/relay/upload"
    env = spool.read_packet(args.seq)
    env["records"][0]["container"] = "ZZZZ0000000"
    print(json.dumps(_send(spool, args.seq,
                           json.dumps(env, ensure_ascii=False), url),
                     ensure_ascii=False, indent=2))


def cmd_bad_record(spool, args):
    """改内容并重算 checksum => 校验和对，但记录非法，整包拒绝。"""
    url = args.url.rstrip("/") + "/relay/upload"
    env = spool.read_packet(args.seq)
    env["records"][0]["slot"] = "99-99-9X"
    env = protocol.seal(env)
    print(json.dumps(_send(spool, args.seq,
                           json.dumps(env, ensure_ascii=False), url),
                     ensure_ascii=False, indent=2))


def build_parser():
    p = argparse.ArgumentParser(description="码头手持终端边缘模拟器")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(s):
        s.add_argument("spool")
        s.add_argument("device_id")

    s = sub.add_parser("init"); add_common(s)
    s.set_defaults(func=lambda spool, a: print(spool.start_session()))

    s = sub.add_parser("scan"); add_common(s)
    s.add_argument("container"); s.add_argument("slot")
    s.add_argument("--at")

    def scan(spool, a):
        rec = spool.append_scan(a.container, a.slot, a.at)
        print(json.dumps(rec, ensure_ascii=False))
    s.set_defaults(func=scan)

    s = sub.add_parser("seal"); add_common(s)
    s.add_argument("--voyage")
    s.set_defaults(func=lambda spool, a: print(spool.seal(a.voyage)))

    s = sub.add_parser("sessions"); add_common(s)
    s.set_defaults(func=lambda spool, a: print(spool.session()))

    def listsegs(spool, a):
        print(json.dumps(spool.segments(), ensure_ascii=False, indent=2))
    s = sub.add_parser("list"); add_common(s)
    s.set_defaults(func=listsegs)

    for name, fn in (("upload", cmd_upload), ("send", cmd_send),
                     ("tamper", cmd_tamper), ("bad-record", cmd_bad_record)):
        s = sub.add_parser(name); add_common(s)
        if name in ("send", "tamper", "bad-record"):
            s.add_argument("seq", type=int)
        s.add_argument("--url", default="http://127.0.0.1:8090")
        s.add_argument("--all", action="store_true")
        s.set_defaults(func=fn)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    spool = DeviceSpool(args.spool, args.device_id)
    args.func(spool, args)


if __name__ == "__main__":
    main()
