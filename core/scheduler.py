"""任务队列 + 调度器。

一条任务 = 一个要抢的商品：
    {id, name, keywords, mode, regions, fire_at, status, note, result,
     min_price, max_price, max_wear, craft, wear, season, sort}

fire_at 是"开卖时刻"（校时后的 Unix 时间戳，0 表示随时可抢/由按钮触发）。

mode 决定工作流：
    detail  详情页模式——停在某件商品页上，盯购买按钮变色（原来的做法）
    list    列表模式——盯交易行搜索结果，挑第一条符合价格/做工/成色条件的
            挂单买。捡漏靠这个：东西什么时候上架、叫什么名字都不知道，
            只能等它出现在列表里。

调度器一个线程，按 fire_at 排序，到点前 ARM_LEAD 秒把引擎切到该任务
的模板，到点触发。多任务串行执行——同一时刻只有一个鼠标，并行没有意义。

状态流转：
    pending → armed → running → done / failed / skipped
"""
import os
import shutil
import threading
import time
import traceback
import uuid
from collections import deque

from . import bricks, ocr, timesync
from .config import Config
from .engine import Engine
from .screen import Screen

PENDING, ARMED, RUNNING, DONE, FAILED, SKIPPED = (
    "pending", "armed", "running", "done", "failed", "skipped")

ARM_LEAD = 20.0      # 提前 20 秒装载模板、进入布防
POLL_IDLE = 0.25


class Scheduler:
    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._engine = Engine()
        self._log = deque(maxlen=400)
        self._active_id = None
        self._cfg = Config.load()

    # ---------------- 队列管理 ----------------

    def tasks(self):
        with self._lock:
            return [dict(t) for t in self._cfg["queue"]]

    def add(self, name, keywords=None, fire_at=0, note="", regions=None, template_from=None,
            mode="detail", min_price=0, max_price=0, max_wear=0,
            craft="", wear="", season="", sort="price"):
        """加一条任务。

        regions 不传就沿用当前框选；template_from 是另一个任务的 id，把它的
        模板复制过来——批量加同一类商品时不用每个都重截一遍。

        mode=list 的任务不需要 buy 模板也能加（列表模式靠 OCR 找行，不靠
        模板匹配），但确认弹窗的模板还是要的。
        """
        if not name or not name.strip():
            return None, "商品名不能为空"
        mode = mode if mode in ("detail", "list") else "detail"
        with self._lock:
            cfg = Config.load()
            regions = regions or {k: v for k, v in cfg["regions"].items() if v}
            need = ("confirm",) if mode == "list" else ("buy", "confirm")
            if mode == "list":
                # 列表模式必须知道盯哪块屏幕，以及点进去后确认按钮在哪
                if not regions.get("list"):
                    return None, "列表模式要先框选交易行列表区域"
            missing = [k for k in need if not regions.get(k)]
            if missing:
                return None, "先把购买和确认按钮框选好再添加任务"
            tid = uuid.uuid4().hex[:8]
            task = {
                "id": tid,
                "name": name.strip(),
                "keywords": [k for k in (keywords or []) if k],
                "mode": mode,
                "regions": regions,
                "fire_at": float(fire_at or 0),
                "status": PENDING,
                "note": note or "",
                "created": time.time(),
                "result": None,
                # 列表模式的筛选条件。0/空 = 不限
                "min_price": float(min_price or 0),
                "max_price": float(max_price or 0),
                "max_wear": float(max_wear or 0),
                "craft": (craft or "").strip(),
                "wear": (wear or "").strip().upper(),
                "season": (season or "").strip().upper(),
                "sort": (sort or "price").strip(),
            }
            cfg["queue"].append(task)
            cfg.save()
            self._cfg = cfg
            self._say(f"已加入队列：{task['name']}"
                      + (f"（{time.strftime('%H:%M:%S', time.localtime(fire_at))} 开卖）"
                         if fire_at else "（手动触发）"))
        if template_from:
            self._copy_templates(template_from, tid)
        else:
            self._snapshot_global_templates(tid)
        return task, None

    def _snapshot_global_templates(self, tid):
        """把当前全局模板存一份给这条任务。

        模板是按商品截的（占位按钮长什么样、确认弹窗长什么样），不同商品
        可能不一样。加任务时快照一份，之后重新框选/重截全局模板也不会把
        已排队的任务改坏。
        """
        cfg = Config.load()
        for name in ("buy", "confirm"):
            src = cfg.template_path(name)
            if os.path.isfile(src):
                shutil.copyfile(src, cfg.template_path(name, tid))

    def add_many(self, names, keywords=None, fire_at=0, interval_s=0, template_from=None,
                 mode="detail", min_price=0, max_price=0, max_wear=0,
                 craft="", wear="", season="", sort="price"):
        """批量加任务。interval_s 是相邻两条的开卖间隔秒数。

        同一批商品通常共用一套按钮模板，所以只快照一次，其余复制第一条的。
        """
        made, errors = [], []
        first_id = template_from
        for i, raw in enumerate(names):
            name = (raw or "").strip()
            if not name:
                continue
            task, err = self.add(
                name,
                keywords=keywords,
                fire_at=(float(fire_at) + i * float(interval_s)) if fire_at else 0,
                template_from=first_id,
                mode=mode,
                min_price=min_price, max_price=max_price, max_wear=max_wear,
                craft=craft, wear=wear, season=season, sort=sort,
            )
            if err:
                errors.append(f"{name}：{err}")
                continue
            made.append(task)
            if not first_id:
                first_id = task["id"]
        return made, errors

    def templates_missing(self, task_id=None, mode="detail"):
        cfg = Config.load()
        names = ("confirm",) if mode == "list" else ("buy", "confirm")
        return [n for n in names
                if not cfg.has_template(n, task_id) and not cfg.has_template(n)]

    def _copy_templates(self, src_id, dst_id):
        """把 src 任务的模板复制给 dst，缺模板时返回缺失的名字。"""
        if src_id == dst_id:
            return []
        cfg = Config.load()
        missing = []
        for name in ("buy", "confirm"):
            src = cfg.template_path(name, src_id)
            if not os.path.isfile(src):
                missing.append(name)
                continue
            shutil.copyfile(src, cfg.template_path(name, dst_id))
        return missing

    def remove(self, task_id):
        with self._lock:
            cfg = Config.load()
            before = len(cfg["queue"])
            cfg["queue"] = [t for t in cfg["queue"] if t["id"] != task_id]
            if len(cfg["queue"]) == before:
                return False, "没有这条任务"
            cfg.save()
            self._cfg = cfg
            return True, "已删除"

    def clear_finished(self):
        with self._lock:
            cfg = Config.load()
            cfg["queue"] = [t for t in cfg["queue"]
                            if t["status"] not in (DONE, FAILED, SKIPPED)]
            cfg.save()
            self._cfg = cfg
            return len(cfg["queue"])

    def reset(self):
        with self._lock:
            cfg = Config.load()
            for t in cfg["queue"]:
                t["status"] = PENDING
                t["result"] = None
            cfg.save()
            self._cfg = cfg

    def update(self, task_id, **fields):
        with self._lock:
            cfg = Config.load()
            for t in cfg["queue"]:
                if t["id"] == task_id:
                    t.update(fields)
                    cfg.save()
                    self._cfg = cfg
                    return True
            return False

    # ---------------- 游戏内读取 ----------------

    def read_product(self):
        """读当前界面上的商品名和价格。返回 {text, name, price, matched}"""
        cfg = Config.load()
        box = cfg.region("name")
        if not box:
            return {"ok": False, "error": "还没框选商品名称区域"}
        s = Screen(cfg["monitor"])
        try:
            img = s.grab_norm(box)
        finally:
            s.close()
        text = ocr.read_text(img)
        price = None
        pbox = cfg.region("price")
        if pbox:
            s2 = Screen(cfg["monitor"])
            try:
                pimg = s2.grab_norm(pbox)
            finally:
                s2.close()
            price = ocr.parse_price(ocr.read_text(pimg))

        candidates = [t["name"] for t in self.tasks()]
        for t in self.tasks():
            candidates.extend(t.get("keywords") or [])
        candidates.extend(bricks.candidates())
        name, score = ocr.best_match(text, candidates) if candidates else (None, 0.0)
        return {"ok": True, "text": text, "price": price,
                "name": name, "score": round(score, 3),
                "brick": bricks.identify(text),
                "candidates": len(candidates)}

    # ---------------- 运行 ----------------

    def start(self, dry_run=True):
        if self._thread and self._thread.is_alive():
            return False, "调度器已在运行"
        with self._lock:
            cfg = Config.load()
            if not cfg["queue"]:
                return False, "队列是空的"
            self._cfg = cfg
        self._stop.clear()
        self._engine = Engine()
        self._thread = threading.Thread(target=self._run, args=(dry_run,), daemon=True)
        self._thread.start()
        return True, "调度器已启动"

    def stop(self):
        self._stop.set()
        self._engine.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        with self._lock:
            for t in self._cfg["queue"]:
                if t["status"] in (ARMED, RUNNING):
                    t["status"] = PENDING
            self._cfg.save()
        return True, "已停止"

    def status(self):
        with self._lock:
            tasks = [dict(t) for t in self._cfg["queue"]]
            active = self._active_id
            log = list(self._log)[-80:]
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "active": active,
            "tasks": tasks,
            "log": log,
            "engine": self._engine.status(),
            "clock": timesync.info(),
        }

    def _say(self, msg, level="info"):
        self._log.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})

    def _set_status(self, task_id, status, result=None):
        with self._lock:
            for t in self._cfg["queue"]:
                if t["id"] == task_id:
                    t["status"] = status
                    if result is not None:
                        t["result"] = result
                    self._cfg.save()
                    break

    def _run(self, dry_run):
        try:
            while not self._stop.is_set():
                pending = [t for t in self.tasks() if t["status"] in (PENDING, ARMED)]
                if not pending:
                    self._say("队列跑完了")
                    break
                # 手动触发的（fire_at=0）就是"现在要抢"，排在所有定时任务前面；
                # 否则排一条一小时后开卖的任务，手动那条会白等一小时。
                pending.sort(key=lambda t: (1, t["fire_at"]) if t["fire_at"] else (0, 0))
                task = pending[0]
                fire_at = task["fire_at"]

                if fire_at:
                    now = timesync.true_now()
                    if task["status"] != ARMED:
                        if now < fire_at - ARM_LEAD:
                            wait = min(fire_at - ARM_LEAD - now, 30.0)
                            self._say(f"下一条：{task['name']}，"
                                      f"{fire_at - now:.0f}s 后进入布防")
                            self._stop.wait(wait)
                            continue
                        self._set_status(task["id"], ARMED)
                        self._say(f"进入布防：{task['name']}，"
                                  f"{fire_at - now:.1f}s 后开卖", "hit")

                # 布防即开跑：引擎一启动就在盯按钮，先确认占位按钮在（布防），
                # 再等它变化。不能等开卖那一刻才启动——那时按钮已经变了，
                # 引擎永远看不到"变化"这个事件。
                self._run_task(task, dry_run)
                if self._stop.is_set():
                    break
        except Exception as e:
            self._say(f"调度器异常：{type(e).__name__}: {e}", "error")
            for line in traceback.format_exc().splitlines()[-6:]:
                self._say(line, "error")
        finally:
            with self._lock:
                self._active_id = None

    def _run_task(self, task, dry_run):
        tid = task["id"]
        mode = task.get("mode") or "detail"
        self._active_id = tid
        self._set_status(tid, RUNNING)
        self._say(f"开始执行：{task['name']}"
                  f"{'（列表模式）' if mode == 'list' else ''}", "hit")

        cfg = Config.load()
        # 把任务的区域和模板套到配置上，引擎只认配置
        for k, v in (task["regions"] or {}).items():
            if k in cfg["regions"]:
                cfg.set_region(k, v)
        # 列表模式的筛选条件也交给引擎：引擎读 active_task 决定买哪一条
        cfg["active_task"] = {
            "name": task.get("name"),
            "keywords": task.get("keywords") or [],
            "min_price": task.get("min_price") or 0,
            "max_price": task.get("max_price") or 0,
            "max_wear": task.get("max_wear") or 0,
            "craft": task.get("craft") or "",
            "wear": task.get("wear") or "",
            "season": task.get("season") or "",
            "sort": task.get("sort") or "price",
        }
        cfg.save()

        # 任务自己的模板复制到全局位置，引擎从那儿读。任务没模板就退回全局的
        # ——加任务时是快照的，正常都有；全局的也缺才是真缺。
        # 列表模式只强依赖确认模板；购买模板只在点进详情页后用到，缺了也能扫。
        need = ("confirm",) if mode == "list" else ("buy", "confirm")
        for name in need:
            src = cfg.template_path(name, tid)
            if not os.path.isfile(src):
                src = cfg.template_path(name)
            if not os.path.isfile(src):
                self._set_status(tid, FAILED, f"缺少 {name} 模板")
                self._say(f"{task['name']}：缺少 {name} 模板，重新截一次", "error")
                return
            shutil.copyfile(src, cfg.template_path(name))
        if mode == "list":
            # 有就带上，没有就不带——引擎在点进详情页时才读它
            src = cfg.template_path("buy", tid)
            if not os.path.isfile(src):
                src = cfg.template_path("buy")
            if os.path.isfile(src):
                shutil.copyfile(src, cfg.template_path("buy"))

        ok, msg = self._engine.start(dry_run=dry_run, deadline=task["fire_at"] or None,
                                     mode=mode)
        if not ok:
            self._set_status(tid, FAILED, msg)
            self._say(f"{task['name']}：{msg}", "error")
            return

        t0 = time.time()
        while not self._stop.is_set():
            st = self._engine.status()
            if st["state"] in ("done", "abort"):
                break
            time.sleep(0.05)

        st = self._engine.status()
        self._engine.stop()
        elapsed = round(time.time() - t0, 2)

        if st["state"] == "done":
            res = f"完成，{st['stats']['clicks']} 次点击，{elapsed}s"
            self._set_status(tid, DONE, res)
            self._say(f"{task['name']}：{res}", "hit")
        elif self._stop.is_set():
            self._set_status(tid, PENDING, "被停止")
            self._say(f"{task['name']}：被停止", "warn")
        else:
            res = st.get("error") or "未抢到"
            self._set_status(tid, FAILED, res)
            self._say(f"{task['name']}：{res}", "warn")
