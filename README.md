# NB on Hugging Face Spaces

NB is a Telegram forwarding automation tool with a Streamlit Web UI.

This README focuses on deploying this project to **Hugging Face Spaces** using **Docker Space**.

## 1. What You Will Deploy

- Web UI entrypoint: `python -m nb.web_ui.run`
- Runtime: Streamlit (headless)
- Port: auto-detected via `PORT` env (HF injects this automatically)
- Main dependencies include:
  - `Telethon`
  - `streamlit`
  - `pymongo`
  - `pytesseract`
  - system packages like `ffmpeg`, `tesseract-ocr` (already in `Dockerfile`)

## 2. Prerequisites

Before deployment, prepare:

1. Telegram API credentials:
   - `API_ID`
   - `API_HASH`
2. Optional bot token if you use bot login mode.
3. A password for Web UI (`PASSWORD`).
4. Optional MongoDB URI (`MONGO_CON_STR`) for persistent state.

## 3. Create a Hugging Face Space

1. Go to Hugging Face and click **New Space**.
2. Choose a name (for example `nb-forwarder`).
3. Select **SDK: Docker**.
4. Set visibility (Public/Private) as needed.
5. Create the Space.

## 4. Push Project to Space Repository

After your Space is created, push this repository content to the Space git remote.

Example flow:

```bash
git remote add hf https://huggingface.co/spaces/<YOUR_USERNAME>/<YOUR_SPACE_NAME>
git push hf main
```

If your default branch is not `main`, push your actual branch.

## 5. Configure Environment Variables (Space Settings)

In your Space page:

- Open **Settings**
- Configure **Variables and Secrets**

Recommended setup:

### Variables

- `PASSWORD`: Web UI password (required)

### Secrets

- `API_ID`: Telegram API ID
- `API_HASH`: Telegram API HASH
- `PHONE_NUMBER`: Telegram phone number for user login (if used)
- `BOT_TOKEN`: Telegram bot token (if bot mode is used)
- `MONGO_CON_STR`: MongoDB connection string (recommended for persistence)

Notes:

- Use **Secrets** for sensitive values (`API_HASH`, `BOT_TOKEN`, etc.).
- `MONGO_CON_STR` is optional but strongly recommended for production use.

## 6. Startup Behavior on Hugging Face

This project is already compatible with HF Docker Spaces:

- `Dockerfile` builds all Python and system dependencies.
- Default command runs:

```bash
python -m nb.web_ui.run
```

- `nb.web_ui.run` reads `PORT` automatically and binds `0.0.0.0`.

You do not need to manually set Streamlit port in Space.

## 7. First Run and Login

1. Wait for Space build to finish.
2. Open Space URL.
3. Enter the Web UI password (`PASSWORD`).
4. Complete Telegram login in the UI:
   - user account mode or bot mode
5. Configure forwarding connections and save.

## 8. Mode Selection (Important)

NB has two running modes:

- `past`: process historical messages
- `live`: process real-time updates

Current behavior is **global mode per process**, not per connection.

That means:

- One process runs either `past` for all enabled connections, or `live` for all enabled connections.
- If you need one connection in `past` and another in `live` at the same time, run two isolated instances (separate config/session/state).

## 9. Persistence and Reliability Recommendations

For stable long-running usage:

1. Set `MONGO_CON_STR` to persist mappings/state.
2. Keep Space private if handling sensitive channels.
3. Avoid frequent rebuilds while forwarding jobs are active.
4. Prefer one clear operation pattern:
   - run `past` once for history
   - then keep `live` running for continuous sync

## 10. Troubleshooting

### Build fails with dependency errors

- Check build logs in Space.
- Ensure no custom changes removed required packages from `Dockerfile`.

### `ModuleNotFoundError: pymongo`

- Confirm image built from current `Dockerfile`.
- Rebuild Space after pushing latest code.

### Web UI not reachable

- Ensure Space SDK is Docker.
- Check app logs for Streamlit startup.
- Confirm process binds `0.0.0.0` and uses injected `PORT`.

### Telegram login/session issues

- Recheck `API_ID` / `API_HASH`.
- If using bot mode, verify `BOT_TOKEN`.
- If account/device is restricted by Telegram, retry later.

### Forwarding delays or FloodWait

- Telegram can impose FloodWait for high-frequency operations.
- Reduce throughput and avoid excessive parallel forwarding.

### OCR/watermark related errors

- Ensure source media type is supported.
- Check plugin configuration in your forwarding settings.

## 11. Local Test Before HF Deploy (Optional)

You can validate quickly in local Docker:

```bash
docker build -t nb-hf .
docker run --rm -p 8501:8501 -e PASSWORD=your_password nb-hf
```

Then open `http://localhost:8501`.

## 12. Security Notes

- Never commit secrets to git.
- Store sensitive keys only in Hugging Face Secrets.
- Rotate tokens if they were exposed.

---

If you want, I can also add a `README.hf-zh.md` version with a shorter, copy-paste style "5-minute deploy" guide.
