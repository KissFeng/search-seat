# search-seat

超星图书馆座位查询和监控脚本。

## 环境配置

```bash
python -m pip install -r requirements.txt
```

Web 服务里的学习通账号、密码、日期和时间都在页面填写。可选系统环境变量只有 `PORT` 和 `NOTIFY_WEBHOOK_URL`。

## 命令行查询

```bash
python main.py --account 学习通账号 --password 学习通密码 --day 2026-07-03 --start 15:00 --end 16:00
```

## Web 监控服务

```bash
python web_service.py
```

打开：

```text
http://127.0.0.1:8000
```

页面里可以设置：

- 学习通账号和密码
- 查询日期和时间段
- 开始时间和结束时间只能选择 08:00-21:00 的整点
- 目标座位号，留空表示任意座位
- 最少命中数量
- 查询间隔，默认 300-360 秒

命中座位会自动停止；到达设置的开始时间仍未命中，也会停止。

## 服务器部署

建议部署在自己的服务器上，用 `systemd` 或进程管理器常驻运行。GitHub Pages 不能运行 Python 后端，也不适合保存学习通账号密码。GitHub Actions 可以做定时任务，但不适合做可交互的 Web 监控页面。
