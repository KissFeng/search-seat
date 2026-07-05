# search-seat

学习通图书馆座位查询 Web 应用。

## 功能

- 使用学习通账号密码登录进入系统
- 登录成功后把学习通 Cookie 保存到本地 MySQL
- 使用已保存 Cookie 请求学习通座位接口
- 查询可预约座位，点击座位号打开官方预约页面
- 计算并展示双人连排座
- 保存最近查询历史，可查看当次座位快照并删除记录
- 开启蹲座位任务，筛选后出现任意可预约座位时发送 Webhook 通知并自动停止
- Webhook 可保存为默认值，下次开启蹲座位时自动填入
- Android WebView 客户端可绑定个推 CID，蹲座命中后通过个推自建通道推送提醒

说明：后端保存的学习通 Cookie 可以用于本项目请求学习通接口，但浏览器从本地页面跳转到 `chaoxing.com` 时，后端不能给第三方域名写入 Cookie。如果浏览器没有登录学习通，官方预约页可能仍会要求登录。

## 环境

```bash
python3 -m pip install -r requirements.txt
```

默认使用本地 MySQL：

```env
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=123456
DB_NAME=search_seat
WATCH_INTERVAL_SECONDS=60
NOTIFY_WEBHOOK_URL=
GETUI_APP_ID=
GETUI_APP_KEY=
GETUI_MASTER_SECRET=
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-password
```

启动时会自动创建数据库和表。

## 启动 Web 应用

```bash
python3 app.py
```

兼容旧命令：

```bash
python3 web_service.py
```

打开：

```text
http://127.0.0.1:8000
```

首次进入直接使用学习通账号密码登录，然后查询座位。

查询表单默认日期为当天；开始时间和结束时间默认不选，必须手动选择，且结束时间必须晚于开始时间。

查询时可选择固定筛选：

- 忽略无电源座位：过滤 `2F-阅览区` 的 `168-211`
- 忽略太阳晒座位：过滤 `2F-阅览区` 的 `348-371`，以及 `3F-阅览区` 的 `284-299`

蹲座位任务会复用当前查询条件和筛选项。任务运行中会按设置的轮询间隔请求学习通；筛选后只要出现任意可预约座位，任务状态变为“已蹲到”并向 Webhook 发送通知；到达预约开始时间仍未蹲到时，任务状态变为“已到点”。

## 数据库表

- `users`：内部用户记录，由学习通账号登录成功后自动创建
- `chaoxing_sessions`：每个学习通账号一条 Cookie
- `seat_query_history`：查询历史和当次座位结果快照
- `seat_watch_tasks`：蹲座位任务、筛选条件、状态和通知结果
- `push_devices`：Android 客户端个推 CID 与内部用户的绑定关系

## 命令行查询

```bash
python3 main.py --account 学习通账号 --password 学习通密码 --day 2026-07-03 --start 15:00 --end 16:00 --show-pairs
```

也可以传浏览器 Cookie：

```bash
python3 main.py --cookie "a=1; b=2" --day 2026-07-03 --start 15:00 --end 16:00
```

## 房间配置

页面里的“房间”下拉框来自 `config.py` 的 `DEFAULT_ROOMS`，当前已按 HAR 抓包配置：

- 2F-阅览区：`12818`，座位 `001-371`
- 3F-阅览区：`12819`，座位 `001-299`
- 4F-阅览区：`12820`，座位 `001-282`
- 2F-24H借阅空间：`11226`，座位 `001-117`

`fidEnc` 仍由后端配置和请求使用，但页面不会展示。需要覆盖房间列表时，可在 `.env` 配置 `ROOMS_JSON`：

```env
ROOMS_JSON=[{"label":"2F-阅览区","room_id":"12818","fid_enc":"087075e03ab2e001","seat_min":1,"seat_max":371,"seat_width":3}]
```

双人连排目前按连续座位号计算，例如 `051 + 052`。如果实际座位图中存在跨行、走道或编号不连续的情况，需要额外补充座位布局表。

## 管理后台

访问：

```text
http://127.0.0.1:8000/admin
```

在 `.env` 配置管理账号：

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-password
```

后台可以查看所有用户、学习通账号、已持久化的学习通姓名、查询历史和蹲座历史，并支持禁用用户。用户首次登录学习通成功后，后端会尝试请求课程接口保存真实姓名；已有老用户如果缺少姓名，可以在后台点击“同步”或“补全缺失姓名”写入数据库。后台日常打开用户列表只读取数据库，不会每次访问都请求学习通。

## Android App 更新

管理后台的“App 更新”页可以上传 Android APK、填写 `versionCode` 和 `versionName`、更新说明，并选择是否强制更新。

Android 客户端进入应用时会请求 `/api/app-update/android` 检查更新。发现新版本后会下载 APK、校验 SHA-256，然后打开 Android 系统安装器。普通 Android 应用不能静默安装 APK，用户仍需要在系统安装界面确认。

APK 文件默认保存到 `uploads/apks/`，可通过环境变量覆盖：

```env
APK_UPLOAD_DIR=/absolute/path/to/apks
```
