---
title: NB
emoji: ✈️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8501
pinned: false
---

# NB on Hugging Face

这个仓库可以直接部署到 Hugging Face Spaces，推荐使用 Docker Space。

## 部署方式

1. 在 Hugging Face 新建一个 Space。
2. SDK 选择 Docker。
3. 将本仓库完整上传到 Space，保留根目录下的 Dockerfile。
4. 可选但强烈建议开启 Persistent Storage，否则页面里的配置文件在重建后会丢失。
5. 等待镜像构建完成，构建成功后会自动启动 Web UI。

## 为什么使用 Docker Space

项目依赖以下系统组件：

- ffmpeg
- tesseract-ocr
- build-essential 等编译依赖

这些依赖已经写在 Dockerfile 里，直接使用 Docker Space 最省事。

## 启动后怎么用

应用启动后会打开 Streamlit 管理页面。

第一次使用时，按下面顺序配置即可：

1. 在 TG 登录 页面填写 Telegram 账号信息。
2. 在 连接 页面添加转发规则。
3. 在 插件 页面按需启用过滤、替换、OCR、水印等功能。
4. 在 运行 页面启动 live、past 或 schedule 模式。

## 数据持久化说明

默认配置会保存在本地文件 nb.config.json 中。

如果 Hugging Face Space 没有开启持久化存储，以下数据可能在重建或重启后丢失：

- 登录信息
- 转发规则
- 插件配置
- 运行参数

## 本地运行

如果要在本地测试：

```bash
pip install -e .
python -m nb.web_ui.run
```

应用默认监听 8501 端口。