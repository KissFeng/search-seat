#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超星座位监控 Web 服务。

运行:
    python web_service.py
"""

import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps, loads
from urllib.parse import urlparse

import requests

import main


TASKS = {}
TASK_LOCK = threading.Lock()
NOTIFY_WEBHOOK_URL = main.get_env("NOTIFY_WEBHOOK_URL")
ALLOWED_HOURS = range(8, 22)
ALLOWED_TIMES = [f"{hour:02d}:00" for hour in ALLOWED_HOURS]


@dataclass
class MonitorTask:
    id: str
    account: str
    password: str
    day: str
    start_time: str
    end_time: str
    desired_seats: list
    min_available: int
    interval_min: int
    interval_max: int
    room_id: str = main.ROOM_ID
    fid_enc: str = main.FID_ENC
    seat_min: int = main.SEAT_MIN
    seat_max: int = main.SEAT_MAX
    seat_width: int = main.SEAT_WIDTH
    status: str = "running"
    message: str = "等待首次查询"
    checks: int = 0
    last_checked_at: str = ""
    next_check_at: str = ""
    available_seats: list = field(default_factory=list)
    matched_seats: list = field(default_factory=list)
    error: str = ""


def parse_seat_list(raw: str, width: int) -> list:
    seats = []
    for item in raw.replace("，", ",").replace(" ", ",").split(","):
        item = item.strip()
        if not item:
            continue
        seats.append(main.normalize_seat_num(item, width))
    return sorted(set(seats))


def parse_start_datetime(task: MonitorTask) -> datetime:
    return datetime.strptime(f"{task.day} {task.start_time}", "%Y-%m-%d %H:%M")


def validate_hour_time(value: str, field_name: str) -> str:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError:
        raise ValueError(f"{field_name} 必须是 HH:00 格式")

    if parsed.minute != 0 or parsed.hour not in ALLOWED_HOURS:
        raise ValueError(f"{field_name} 只能选择 08:00 到 21:00 的整点")
    return f"{parsed.hour:02d}:00"


def validate_time_range(start_time: str, end_time: str) -> tuple:
    start = validate_hour_time(start_time, "开始时间")
    end = validate_hour_time(end_time, "结束时间")
    if end <= start:
        raise ValueError("结束时间必须晚于开始时间")
    return start, end


def task_snapshot(task: MonitorTask) -> dict:
    data = asdict(task)
    data.pop("password", None)
    if data.get("account"):
        data["account"] = mask_account(data["account"])
    data["stopped"] = task.status in {"found", "expired", "stopped", "error"}
    return data


def mask_account(account: str) -> str:
    if len(account) <= 4:
        return "*" * len(account)
    return f"{account[:2]}{'*' * (len(account) - 4)}{account[-2:]}"


def save_task(task: MonitorTask) -> None:
    with TASK_LOCK:
        TASKS[task.id] = task


def get_task(task_id: str) -> MonitorTask:
    with TASK_LOCK:
        return TASKS.get(task_id)


def list_tasks() -> list:
    with TASK_LOCK:
        return [task_snapshot(task) for task in TASKS.values()]


def notify(task: MonitorTask) -> None:
    if not NOTIFY_WEBHOOK_URL:
        return

    payload = {
        "title": "找到可预约座位",
        "message": task.message,
        "task": task_snapshot(task),
    }
    try:
        requests.post(NOTIFY_WEBHOOK_URL, json=payload, timeout=8)
    except requests.RequestException:
        pass


def check_once(task: MonitorTask, session: requests.Session) -> None:
    result = main.query_seats(
        room_id=task.room_id,
        fid_enc=task.fid_enc,
        day=task.day,
        start_time=task.start_time,
        end_time=task.end_time,
        session=session,
    )
    if not result.get("success"):
        raise RuntimeError(f"查询失败：{result}")

    all_seats = main.build_all_seats(task.seat_min, task.seat_max, task.seat_width)
    available = main.get_available_seats(result, all_seats, task.seat_width)
    matched = (
        [seat for seat in available if seat in set(task.desired_seats)]
        if task.desired_seats
        else available
    )

    task.checks += 1
    task.last_checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task.available_seats = available
    task.matched_seats = matched
    task.error = ""

    if len(matched) >= task.min_available:
        task.status = "found"
        task.message = (
            f"命中 {len(matched)} 个座位：{', '.join(matched[:30])}"
            + (" ..." if len(matched) > 30 else "")
        )
        notify(task)
    else:
        target_desc = (
            f"目标座位 {', '.join(task.desired_seats)}"
            if task.desired_seats
            else f"任意可预约座位 >= {task.min_available} 个"
        )
        task.message = f"未命中，当前可预约 {len(available)} 个，条件：{target_desc}"


def monitor_loop(task_id: str) -> None:
    task = get_task(task_id)
    if not task:
        return

    session = None
    while task.status == "running":
        if datetime.now() >= parse_start_datetime(task):
            task.status = "expired"
            task.message = "已到预约开始时间，停止监控"
            task.next_check_at = ""
            save_task(task)
            return

        try:
            session = session or main.login(task.account, task.password)
            check_once(task, session)
        except Exception as exc:
            task.checks += 1
            task.last_checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            task.error = str(exc)
            task.message = f"查询出错：{exc}"
            session = None

        if task.status != "running":
            task.next_check_at = ""
            save_task(task)
            return

        delay = random.randint(task.interval_min, task.interval_max)
        task.next_check_at = datetime.fromtimestamp(time.time() + delay).strftime("%Y-%m-%d %H:%M:%S")
        save_task(task)

        for _ in range(delay):
            time.sleep(1)
            if task.status != "running":
                task.next_check_at = ""
                save_task(task)
                return


def create_task(payload: dict) -> dict:
    account = str(payload.get("account") or "").strip()
    password = str(payload.get("password") or "").strip()
    if not account:
        return {"error": "请填写学习通账号", "status": 400}
    if not password:
        return {"error": "请填写学习通密码", "status": 400}

    day = str(payload.get("day") or "").strip()
    if not day:
        return {"error": "请选择日期", "status": 400}
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return {"error": "日期必须是 YYYY-MM-DD 格式", "status": 400}

    interval_min = int(payload.get("interval_min") or 300)
    interval_max = int(payload.get("interval_max") or 360)
    if interval_min < 30:
        return {"error": "interval_min 不能小于 30 秒", "status": 400}
    if interval_max < interval_min:
        return {"error": "interval_max 不能小于 interval_min", "status": 400}

    try:
        start_time, end_time = validate_time_range(
            str(payload.get("start_time") or "").strip(),
            str(payload.get("end_time") or "").strip(),
        )
    except ValueError as exc:
        return {"error": str(exc), "status": 400}

    task = MonitorTask(
        id=str(uuid.uuid4())[:8],
        account=account,
        password=password,
        day=day,
        start_time=start_time,
        end_time=end_time,
        desired_seats=parse_seat_list(payload.get("desired_seats", ""), main.SEAT_WIDTH),
        min_available=max(1, int(payload.get("min_available") or 1)),
        interval_min=interval_min,
        interval_max=interval_max,
    )
    save_task(task)

    thread = threading.Thread(target=monitor_loop, args=(task.id,), daemon=True)
    thread.start()
    return {"task": task_snapshot(task), "status": 200}


def stop_task(task_id: str) -> dict:
    task = get_task(task_id)
    if not task:
        return {"error": "任务不存在", "status": 404}
    if task.status == "running":
        task.status = "stopped"
        task.message = "已手动停止"
        task.next_check_at = ""
        save_task(task)
    return {"task": task_snapshot(task), "status": 200}


class SeatMonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_text(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, body: dict) -> None:
        self.send_text(status, dumps(body, ensure_ascii=False), "application/json; charset=utf-8")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_text(200, INDEX_HTML)
        elif path == "/api/tasks":
            self.send_json(200, {"tasks": list_tasks()})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/tasks":
                result = create_task(self.read_json())
                status = result.pop("status")
                self.send_json(status, result)
                return

            prefix = "/api/tasks/"
            suffix = "/stop"
            if path.startswith(prefix) and path.endswith(suffix):
                task_id = path[len(prefix):-len(suffix)]
                result = stop_task(task_id)
                status = result.pop("status")
                self.send_json(status, result)
                return

            self.send_json(404, {"error": "not found"})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})


INDEX_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>座位监控</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #1f2937; }
    main { max-width: 1080px; margin: 0 auto; padding: 28px 18px 42px; }
    h1 { margin: 0 0 18px; font-size: 28px; }
    section { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 18px; margin-bottom: 18px; }
    form { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; align-items: end; }
    label { display: grid; gap: 6px; font-size: 13px; color: #4b5563; }
    input, select { width: 100%; box-sizing: border-box; border: 1px solid #d1d5db; border-radius: 6px; padding: 9px 10px; font-size: 14px; background: #fff; }
    .wide { grid-column: span 2; }
    button { border: 0; border-radius: 6px; background: #0f766e; color: white; padding: 10px 14px; font-size: 14px; cursor: pointer; }
    button.secondary { background: #475569; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e5e7eb; padding: 10px 8px; text-align: left; vertical-align: top; font-size: 14px; }
    th { color: #475569; font-weight: 600; background: #f8fafc; }
    .status { display: inline-block; min-width: 54px; border-radius: 999px; padding: 3px 8px; color: #fff; text-align: center; font-size: 12px; }
    .running { background: #2563eb; }
    .found { background: #16a34a; }
    .expired, .stopped { background: #64748b; }
    .error { background: #dc2626; }
    .seats { max-width: 280px; word-break: break-word; color: #166534; }
    .hint { color: #64748b; font-size: 13px; margin: 6px 0 0; }
    @media (max-width: 760px) {
      form { grid-template-columns: 1fr; }
      .wide { grid-column: span 1; }
      table { display: block; overflow-x: auto; }
    }
  </style>
</head>
<body>
<main>
  <h1>座位监控</h1>
  <section>
    <form id="taskForm">
      <label>学习通账号
        <input name="account" autocomplete="username" required>
      </label>
      <label>学习通密码
        <input name="password" type="password" autocomplete="current-password" required>
      </label>
      <label>日期
        <input name="day" type="date" required>
      </label>
      <label>开始时间
        <select name="start_time" required></select>
      </label>
      <label>结束时间
        <select name="end_time" required></select>
      </label>
      <label>至少命中数量
        <input name="min_available" type="number" min="1" value="1">
      </label>
      <label class="wide">目标座位，可空
        <input name="desired_seats" placeholder="例如 054,075,163；留空表示任意座位">
      </label>
      <label>最短间隔/秒
        <input name="interval_min" type="number" min="30" value="300">
      </label>
      <label>最长间隔/秒
        <input name="interval_max" type="number" min="30" value="360">
      </label>
      <button type="submit">开始监控</button>
    </form>
    <p class="hint">命中后会自动停止；如果到达设置的开始时间仍未命中，也会停止。</p>
  </section>

  <section>
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>状态</th>
          <th>时间段</th>
          <th>条件</th>
          <th>结果</th>
          <th>检查</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id="taskRows"></tbody>
    </table>
  </section>
</main>
<script>
const form = document.querySelector('#taskForm');
const rows = document.querySelector('#taskRows');
const notified = new Set();
const allowedTimes = Array.from({ length: 14 }, (_, index) => `${String(index + 8).padStart(2, '0')}:00`);

function today() {
  return new Date().toISOString().slice(0, 10);
}

form.day.value = today();
form.start_time.innerHTML = allowedTimes.map(time => `<option value="${time}">${time}</option>`).join('');
form.end_time.innerHTML = allowedTimes.map(time => `<option value="${time}">${time}</option>`).join('');
form.start_time.value = '15:00';
form.end_time.value = '16:00';

async function ensureNotificationPermission() {
  if (!('Notification' in window)) return;
  if (Notification.permission === 'default') {
    await Notification.requestPermission();
  }
}

function maybeNotify(task) {
  if (task.status !== 'found' || notified.has(task.id)) return;
  notified.add(task.id);
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification('找到可预约座位', { body: task.message });
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function render(tasks) {
  rows.innerHTML = tasks.map(task => {
    const condition = task.desired_seats.length
      ? `目标 ${escapeHtml(task.desired_seats.join(', '))}`
      : `任意座位 >= ${task.min_available}`;
    const result = task.matched_seats.length
      ? `<div class="seats">${escapeHtml(task.matched_seats.join(', '))}</div>`
      : escapeHtml(task.message);
    const action = task.status === 'running'
      ? `<button class="secondary" onclick="stopTask('${task.id}')">停止</button>`
      : '';
    maybeNotify(task);
    return `<tr>
      <td>${task.id}</td>
      <td><span class="status ${task.status}">${task.status}</span></td>
      <td>${escapeHtml(task.day)} ${escapeHtml(task.start_time)}-${escapeHtml(task.end_time)}</td>
      <td>${condition}</td>
      <td>${result}</td>
      <td>已查 ${task.checks} 次<br>上次：${escapeHtml(task.last_checked_at || '-')}<br>下次：${escapeHtml(task.next_check_at || '-')}</td>
      <td>${action}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="7">暂无任务</td></tr>';
}

async function loadTasks() {
  const res = await fetch('/api/tasks');
  const data = await res.json();
  render(data.tasks);
}

async function stopTask(id) {
  await fetch(`/api/tasks/${id}/stop`, { method: 'POST' });
  await loadTasks();
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  await ensureNotificationPermission();
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.min_available = Number(payload.min_available);
  payload.interval_min = Number(payload.interval_min);
  payload.interval_max = Number(payload.interval_max);
  const res = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || '创建任务失败');
    return;
  }
  await loadTasks();
});

loadTasks();
setInterval(loadTasks, 3000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(main.get_env("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), SeatMonitorHandler)
    print(f"座位监控服务已启动：http://127.0.0.1:{port}")
    server.serve_forever()
