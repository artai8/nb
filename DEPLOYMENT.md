# 生产环境部署指南

1. 复制 `.env.example` 为 `.env`，配置 `API_ID`、`DOMAIN`、`PUBLIC_BASE_URL` 以及初始管理员参数（`NB_ADMIN_USERNAME`、`NB_ADMIN_PASSWORD`、`NB_ADMIN_PATH`）。
2. 按照 `secrets/README.md` 在 `secrets/` 目录下创建对应的密钥文件。
3. 运行 `docker compose up -d --build` 启动 `gateway` (Caddy)、`mongo`、`api`、`bot` 与 `worker`。
4. 普通租户访问 `https://<域名>/miniapp`，管理员访问 `https://<域名>/admin`。
