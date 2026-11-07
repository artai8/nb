# nb/web_ui/utils.py
"""Liquid Glass UI Kit Component Library & Global Styles for Streamlit Pages (1:1 Refactored from 123.jpg)."""

from __future__ import annotations
import streamlit as st


def jelly_svg(icon_name: str, size: int = 36) -> str:
    """Generate inline SVG for Liquid Glass 3D Duotone icons (Violet #7357F6 + Aqua #52C5CF)."""
    icons = {
        "rocket": """
            <rect width="48" height="48" rx="16" fill="white" fill-opacity="0.92"/>
            <path d="M16 14.5C16 13.12 17.12 12 18.5 12H29.5C33.64 12 37 15.36 37 19.5C37 23.64 33.64 27 29.5 27H21V34.5C21 35.88 19.88 37 18.5 37C17.12 37 16 35.88 16 34.5V14.5Z" fill="#7357F6"/>
            <circle cx="31.5" cy="31.5" r="5.5" fill="#52C5CF"/>
        """,
        "telegram": """
            <rect width="48" height="48" rx="16" fill="white" fill-opacity="0.92"/>
            <path d="M36.5 13.5L11.5 23.2C10.4 23.6 10.4 25.1 11.6 25.5L18.2 27.6L32.2 18.5L21.4 29.2V34.8C21.4 35.9 22.8 36.4 23.5 35.5L26.8 31.6L33.2 36.2C34.2 36.9 35.6 36.3 35.9 35.1L38.4 15.1C38.6 13.9 37.5 13.1 36.5 13.5Z" fill="#7357F6"/>
            <circle cx="33" cy="16" r="4.5" fill="#52C5CF"/>
        """,
        "shield": """
            <rect width="48" height="48" rx="16" fill="white" fill-opacity="0.92"/>
            <path d="M24 11L13 15.5V23.5C13 30.8 17.7 37.3 24 39.5C30.3 37.3 35 30.8 35 23.5V15.5L24 11Z" fill="#7357F6"/>
            <path d="M20 24.5L23 27.5L29.5 21" stroke="#52C5CF" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
        """,
        "link": """
            <rect width="48" height="48" rx="16" fill="white" fill-opacity="0.92"/>
            <path d="M20 28L28 20M17.5 22.5L15 25C12.8 27.2 12.8 30.8 15 33C17.2 35.2 20.8 35.2 23 33L25.5 30.5" stroke="#7357F6" stroke-width="3.2" stroke-linecap="round"/>
            <path d="M30.5 25.5L33 23C35.2 20.8 35.2 17.2 33 15C30.8 12.8 27.2 12.8 25 15L22.5 17.5" stroke="#52C5CF" stroke-width="3.2" stroke-linecap="round"/>
        """,
        "bolt": """
            <rect width="48" height="48" rx="16" fill="white" fill-opacity="0.92"/>
            <path d="M26.5 11L14.5 26H24L21.5 37L33.5 22H24L26.5 11Z" fill="#7357F6"/>
            <circle cx="32.5" cy="31.5" r="4.5" fill="#52C5CF"/>
        """,
        "plugin": """
            <rect width="48" height="48" rx="16" fill="white" fill-opacity="0.92"/>
            <rect x="13" y="13" width="10" height="10" rx="3" fill="#7357F6"/>
            <rect x="25" y="13" width="10" height="10" rx="3" fill="#52C5CF"/>
            <rect x="13" y="25" width="10" height="10" rx="3" fill="#52C5CF"/>
            <rect x="25" y="25" width="10" height="10" rx="3" fill="#7357F6"/>
        """,
        "settings": """
            <rect width="48" height="48" rx="16" fill="white" fill-opacity="0.92"/>
            <circle cx="24" cy="24" r="8" stroke="#7357F6" stroke-width="3.5"/>
            <circle cx="24" cy="24" r="3.5" fill="#52C5CF"/>
        """,
    }
    body = icons.get(icon_name, icons["rocket"])
    return f'<svg width="{size}" height="{size}" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg" style="filter: drop-shadow(0 6px 14px rgba(108,79,246,0.18));">{body}</svg>'


def switch_theme(st_obj, config: dict | None = None) -> None:
    """Inject full Liquid Glass UI Kit CSS (matching 123.jpg) into Streamlit pages."""
    css = """
    <style>
      /* 全局斜射暖米白与冷蓝灰渐变画布 */
      .stApp {
        background: linear-gradient(135deg, #f3e9e2 0%, #e4e1ea 48%, #c9cedc 100%) !important;
        color: #1d1e2c !important;
        font-family: "Inter", -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif !important;
      }

      /* 侧边栏液态磨砂玻璃化 */
      section[data-testid="stSidebar"] {
        background: linear-gradient(160deg, rgba(255, 255, 255, 0.65) 0%, rgba(242, 245, 255, 0.38) 100%) !important;
        backdrop-filter: blur(28px) saturate(180%) !important;
        -webkit-backdrop-filter: blur(28px) saturate(180%) !important;
        border-right: 1.5px solid rgba(255, 255, 255, 0.85) !important;
        box-shadow: 12px 0 36px rgba(36, 42, 68, 0.12) !important;
      }

      /* 主容器包裹为大圆角透明亚克力玻璃画板 (Main Glass Slab) */
      .block-container {
        max-width: 1120px !important;
        margin: 26px auto 48px !important;
        padding: 34px 38px !important;
        background: linear-gradient(145deg, rgba(255, 255, 255, 0.54) 0%, rgba(255, 255, 255, 0.24) 100%) !important;
        backdrop-filter: blur(30px) saturate(180%) !important;
        -webkit-backdrop-filter: blur(30px) saturate(180%) !important;
        border-radius: 34px !important;
        border: 1.5px solid rgba(255, 255, 255, 0.86) !important;
        box-shadow:
          0 32px 68px -12px rgba(36, 42, 68, 0.24),
          0 14px 28px -8px rgba(36, 42, 68, 0.12),
          inset 0 2px 3px rgba(255, 255, 255, 0.94),
          inset 0 -2px 4px rgba(168, 180, 212, 0.36) !important;
      }

      /* 液态玻璃卡片组件 */
      .glass-card {
        background: linear-gradient(145deg, rgba(255, 255, 255, 0.66) 0%, rgba(245, 248, 255, 0.35) 100%);
        backdrop-filter: blur(22px);
        -webkit-backdrop-filter: blur(22px);
        border: 1.5px solid rgba(255, 255, 255, 0.86);
        border-radius: 24px;
        padding: 22px 24px;
        margin-bottom: 18px;
        box-shadow:
          0 16px 34px -8px rgba(45, 53, 86, 0.15),
          inset 0 1.5px 2px rgba(255, 255, 255, 0.92),
          inset 0 -1.5px 3px rgba(175, 190, 225, 0.32);
      }

      .iridescent-card {
        background: linear-gradient(135deg, rgba(255, 198, 178, 0.78) 0%, rgba(204, 175, 255, 0.75) 52%, rgba(142, 228, 248, 0.82) 100%);
        backdrop-filter: blur(24px);
        -webkit-backdrop-filter: blur(24px);
        border: 1.5px solid rgba(255, 255, 255, 0.92);
        border-radius: 24px;
        padding: 22px 24px;
        margin-bottom: 18px;
        box-shadow:
          0 20px 42px -10px rgba(108, 79, 246, 0.26),
          0 8px 20px rgba(72, 189, 198, 0.2),
          inset 0 2px 4px rgba(255, 255, 255, 0.94);
      }

      .aqua-card {
        background: linear-gradient(135deg, rgba(210, 246, 248, 0.72) 0%, rgba(196, 206, 255, 0.58) 65%, rgba(176, 168, 252, 0.68) 100%);
        backdrop-filter: blur(22px);
        border: 1.5px solid rgba(255, 255, 255, 0.88);
        border-radius: 24px;
        padding: 22px 24px;
        margin-bottom: 18px;
        box-shadow: 0 18px 36px -8px rgba(68, 92, 160, 0.2), inset 0 2px 3px #ffffff;
      }

      .status-pill {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 11px 18px;
        border-radius: 9999px;
        background: linear-gradient(180deg, rgba(255, 255, 255, 0.78) 0%, rgba(242, 244, 250, 0.48) 100%);
        border: 1.5px solid rgba(255, 255, 255, 0.88);
        box-shadow: 0 10px 24px -6px rgba(45, 53, 86, 0.12), inset 0 1.5px 2px #ffffff;
        margin-bottom: 12px;
        font-weight: 700;
        color: #1d1e2c;
      }

      /* Streamlit 原生按钮改造为 3D 液态紫罗兰胶囊 (Primary Button) */
      div.stButton > button,
      div[data-testid="stFormSubmitButton"] > button {
        background: linear-gradient(180deg, #9f85ff 0%, #7357f6 55%, #6246ea 100%) !important;
        color: #ffffff !important;
        border: 1px solid rgba(255, 255, 255, 0.65) !important;
        border-radius: 9999px !important;
        padding: 10px 26px !important;
        font-weight: 700 !important;
        letter-spacing: 0.01em !important;
        box-shadow:
          0 12px 24px -6px rgba(108, 79, 246, 0.46),
          0 4px 10px rgba(108, 79, 246, 0.26),
          inset 0 2px 3px rgba(255, 255, 255, 0.8),
          inset 0 -3px 6px rgba(62, 34, 180, 0.45) !important;
        transition: all 0.18s ease !important;
      }

      div.stButton > button:hover,
      div[data-testid="stFormSubmitButton"] > button:hover {
        transform: translateY(-1.5px) !important;
        filter: brightness(1.06) !important;
      }

      /* Streamlit 输入框改造为珠光白胶囊输入框 */
      div[data-baseweb="input"] > div,
      div[data-baseweb="textarea"] > div,
      div[data-baseweb="select"] > div {
        background: rgba(255, 255, 255, 0.86) !important;
        border-radius: 9999px !important;
        border: 1.5px solid rgba(255, 255, 255, 0.95) !important;
        box-shadow: 0 6px 16px rgba(45, 53, 86, 0.08), inset 0 1px 2px #ffffff !important;
        color: #1d1e2c !important;
      }

      /* Metric 指标卡液态玻璃化 */
      div[data-testid="stMetric"] {
        background: linear-gradient(145deg, rgba(255, 255, 255, 0.72) 0%, rgba(245, 248, 255, 0.42) 100%);
        border: 1.5px solid rgba(255, 255, 255, 0.88);
        border-radius: 22px;
        padding: 16px 20px;
        box-shadow: 0 12px 28px -8px rgba(45, 53, 86, 0.14), inset 0 1.5px 2px #ffffff;
      }

      .badge {
        background: linear-gradient(180deg, #8ae2e6 0%, #46b1ba 100%);
        color: #ffffff;
        padding: 4px 12px;
        border-radius: 9999px;
        font-size: 0.45em;
        font-weight: 700;
        vertical-align: middle;
        box-shadow: 0 4px 12px rgba(72, 189, 198, 0.38), inset 0 1px 2px rgba(255,255,255,0.8);
      }
    </style>
    """
    st_obj.markdown(css, unsafe_allow_html=True)


def render_jelly_hero(
    st_obj,
    title: str,
    subtitle: str,
    icon: str = "rocket",
    badge: str = "",
) -> None:
    st_obj.markdown(
        f"""
        <div class="aqua-card" style="margin-bottom: 24px;">
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;">
            <div style="display: flex; align-items: center; gap: 16px;">
              {jelly_svg(icon, 52)}
              <div>
                <h1 style="margin:0; font-size: 1.75rem; font-weight: 800; color: #1d1e2c; letter-spacing: -0.02em;">
                  {title} {f'<span class="badge">{badge}</span>' if badge else ''}
                </h1>
                <p style="margin: 4px 0 0 0; color: #5a607a; font-size: 0.94rem; font-weight: 500;">{subtitle}</p>
              </div>
            </div>
            <div style="display:flex; gap:10px; align-items:center;">
              <span style="width:36px; height:36px; border-radius:50%; background:rgba(255,255,255,0.75); border:1.5px solid #fff; display:inline-flex; align-items:center; justify-content:center; color:#5a607a; font-weight:700; box-shadow:0 4px 10px rgba(36,42,68,0.1);">↻</span>
              <span style="width:36px; height:36px; border-radius:50%; background:rgba(255,255,255,0.75); border:1.5px solid #fff; display:inline-flex; align-items:center; justify-content:center; color:#5a607a; font-weight:700; box-shadow:0 4px 10px rgba(36,42,68,0.1);">—</span>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
