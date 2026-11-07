# NB 多租户 Telegram 转发系统 (v3.4 Liquid Glass Edition)

本项目由三个核心运行服务组成（已移除冗余的 `dashboard` 容器与 `secrets/nb/` 重复目录）：

- `api`：FastAPI 控制面、普通租户端 (`/miniapp`) 与独立超级管理员后台 (`/admin`)。
- `bot`：Telegram 私聊控制机器人（支持一键共享本机号码、免前缀验证码识别、`/session` 导入、`/qr` 自动刷新二维码与永久有效 `/web` 登录链接）。
- `worker`：基于 MongoDB 分布式租约 (`worker_leases`) 的多租户转发引擎，支持横向扩容。

## 核心特性

1. **永久有效登录链接与无状态验证码挑战**：已移除 5 分钟过期限制，支持多端重复打开；网页端发送验证码的临时会话加密持久化于 MongoDB，支持多实例部署。
2. **5 种免扫码/单机友好登录方式**：网页手机号验证码、Telethon `StringSession` 字符串导入、上传 `.session` 文件登录、`tg://login` 客户端一键唤起授权、Bot 私聊「一键共享本机号码」。
3. **物理隔离的超级管理员后台 (`/admin`)**：
   - 普通租户访问 `/miniapp`，完全看不到任何管理员入口或提示，支持新用户自助注册。
   - 管理员通过 `/admin`（支持在后台自由更改管理员账号名、密码以及后台路径如 `/my-console`）登录，可跨租户管理所有租户账号、任务配额 (`max_tasks`)、高算力插件开关 (`allow_heavy_plugins`) 以及任意租户下的全部转发任务与 8 大插件参数。
4. **已加入频道/群组可视化下拉选择器 + 插件实时沙盒预览测试器 + 实时投递流水监控**。

## 快速启动

```bash
cp .env.example .env
docker compose up -d --build
```
