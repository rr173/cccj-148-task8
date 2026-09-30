"""边缘侧：手持终端的只追加本地段文件。

目录布局（spool 根目录下）：

    <device_id>/
      session.txt                 # 当前会话标识（设备每次“上线开工”新建）
      current.seg                 # 当前开放段（只追加 JSONL）
      sealed/000007.json          # 已封段 = 待上传包（不可变）
      state.json                  # 每段上传状态 pending|sent|acked

硬保证：
- current.seg 与 sealed/*.json 只追加/只新建，从不原地改写；
- 每条扫描落盘 flush+fsync 后才向终端返回成功；
- 封段把 JSONL 原样收进带 checksum 的信封并改名（rename 原子）。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from . import protocol


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class DeviceSpool:
    def __init__(self, root: str, device_id: str):
        self.root = root
        self.device_id = device_id
        self.dir = os.path.join(root, device_id)
        self.sealed_dir = os.path.join(self.dir, "sealed")
        os.makedirs(self.sealed_dir, exist_ok=True)

    # ----- 会话 ----------------------------------------------------------- #
    def start_session(self, session: str | None = None) -> str:
        path = os.path.join(self.dir, "session.txt")
        if session is None:
            session = f"{self.device_id}:{utc_now_iso()}"
        # 切换会话必须先封闭当前段
        cur = os.path.join(self.dir, "current.seg")
        if os.path.exists(cur):
            self.seal()
        with open(path, "w", encoding="utf-8") as f:
            f.write(session)
            f.flush()
            os.fsync(f.fileno())
        return session

    def session(self) -> str:
        path = os.path.join(self.dir, "session.txt")
        if not os.path.exists(path):
            return self.start_session()
        with open(path, encoding="utf-8") as f:
            return f.read().strip()

    def _state_path(self) -> str:
        return os.path.join(self.dir, "state.json")

    def _load_state(self) -> dict:
        p = self._state_path()
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_state(self, state: dict) -> None:
        _atomic_write_json(self._state_path(), state)

    # ----- 扫描写入 ------------------------------------------------------- #
    def append_scan(self, container: str, slot: str,
                    collected_at: str | None = None,
                    record_id: str | None = None) -> dict:
        """追加一条扫描到当前开放段（只追加；落盘 fsync 后返回）。"""
        record = {
            "record_id": record_id or f"rec_{uuid.uuid4().hex}",
            "container": container,
            "slot": slot,
            "collected_at": collected_at or utc_now_iso(),
        }
        cur = os.path.join(self.dir, "current.seg")
        with open(cur, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return record

    # ----- 封段 / 读取 ---------------------------------------------------- #
    def _next_seq(self, state) -> int:
        seqs = [int(k) for k in state.keys()]
        return (max(seqs) + 1) if seqs else 0

    def seal(self, voyage_id: str | None = None) -> int:
        """把当前开放段封闭为不可变上传包，返回段序号。"""
        cur = os.path.join(self.dir, "current.seg")
        if not os.path.exists(cur) or os.path.getsize(cur) == 0:
            raise FileNotFoundError("当前没有可封闭的扫描段")
        state = self._load_state()
        seq = self._next_seq(state)
        records = []
        with open(cur, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        envelope = protocol.seal({
            "device_id": self.device_id,
            "session": self.session(),
            "seq": seq,
            "voyage_id": voyage_id,
            "records": records,
        })
        dest = os.path.join(self.sealed_dir, f"{seq:06d}.json")
        _atomic_write_json(dest, envelope)
        os.remove(cur)  # 段内容已完整落到 sealed 下
        state[str(seq)] = {"status": "pending", "file": os.path.basename(dest),
                           "attempts": 0}
        self._save_state(state)
        return seq

    def read_packet(self, seq: int) -> dict:
        with open(os.path.join(self.sealed_dir, f"{seq:06d}.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def segments(self) -> list[dict]:
        """按序号返回全部已封段及其上传状态。"""
        state = self._load_state()
        out = []
        for seq_str, meta in sorted(state.items(), key=lambda kv: int(kv[0])):
            out.append({"seq": int(seq_str), **meta})
        return out

    def pending_seqs(self) -> list[int]:
        """待传（含已发送未确认）；按序号严格递增上传。"""
        return [s["seq"] for s in self.segments()
                if s["status"] in ("pending", "sent")]

    def next_seq(self) -> int | None:
        seqs = self.pending_seqs()
        return seqs[0] if seqs else None

    def mark_sent(self, seq: int) -> None:
        state = self._load_state()
        m = state[str(seq)]
        m["status"] = "sent"
        m["attempts"] += 1
        self._save_state(state)

    def mark_acked(self, seq: int, ack: dict) -> None:
        state = self._load_state()
        state[str(seq)]["status"] = "acked"
        state[str(seq)]["ack"] = ack
        self._save_state(state)

    # ----- 演练用：篡改与补造 --------------------------------------------- #
    def tamper_packet(self, seq: int, mutate) -> str:
        """只读原包→产出被篡改字节（重封/不重封由 mutate 决定）。

        原 sealed 文件保持不动；返回应实际发送出去的 JSON 文本。
        """
        env = self.read_packet(seq)
        return mutate(env)
