# nb/web_ui/0_👋_Hello.py

import streamlit as st
from nb.web_ui.utils import hide_st, switch_theme
from nb.config import read_config

CONFIG = read_config()

st.set_page_config(
    page_title="NB - Home",
    page_icon="👋",
    layout="wide"
)
switch_theme(st, CONFIG)

# --- Hero Header ---
col_logo, col_txt = st.columns([1, 6])
with col_logo:
    st.image("https://user-images.githubusercontent.com/66209958/115183360-3fa4d500-a0f9-11eb-9c0f-c5ed03a9ae17.png", width=100)
with col_txt:
    st.markdown("""
    <div style='padding-top: 10px;'>
        <h1 style='margin-bottom: 0; font-size: 2.5rem; background: -webkit-linear-gradient(45deg, #6366f1, #ec4899); -webkit-background-clip: text; -webkit-text-fill-color: transparent;'>Welcome to NB Manager</h1>
        <p style='color: #64748b; font-size: 1.1rem;'>The Ultimate Telegram Forwarding Tool</p>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# --- Feature Cards ---
st.markdown("### 🚀 Capabilities")
c1, c2, c3 = st.columns(3)

def card(icon, title, desc, color):
    st.markdown(f"""
    <div style="
        background: white; 
        padding: 20px; 
        border-radius: 12px; 
        border: 1px solid #e2e8f0; 
        box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);
        height: 100%;
    ">
        <div style="
            width: 40px; height: 40px; 
            background: {color}20; 
            border-radius: 8px; 
            display: flex; align-items: center; justify-content: center;
            font-size: 1.2rem; margin-bottom: 12px;
        ">
            {icon}
        </div>
        <h3 style="margin: 0 0 8px 0; font-size: 1.1rem;">{title}</h3>
        <p style="margin: 0; color: #64748b; font-size: 0.9rem; line-height: 1.5;">{desc}</p>
    </div>
    """, unsafe_allow_html=True)

with c1:
    card("📤", "Smart Forwarding", "Automate message forwarding between Channels, Groups, and Bots with ease.", "#6366f1")
with c2:
    card("⚡", "Live & Past Modes", "Sync real-time messages or migrate chat history with offset controls.", "#10b981")
with c3:
    card("🧩", "Powerful Plugins", "Filter, Replace, Watermark, OCR, Format, and modify messages on the fly.", "#f59e0b")

st.markdown("---")

# --- Quick Links ---
c_info, c_dev = st.columns([2, 1])

with c_info:
    st.info("""
    **Getting Started?**
    Check the sidebar menu to configure your **Telegram Login**, set up **Connections**, and start the **Run Dashboard**.
    """)
    st.markdown("[View Documentation on GitHub Wiki](https://github.com/artai8/nb/wiki)")

with c_dev:
    st.markdown("""
    <div style="background: #f1f5f9; padding: 15px; border-radius: 10px; border-left: 4px solid #6366f1;">
        <small style="color:#475569"><b>Developer Note:</b><br>
        Plugins allow you to modify messages before they are sent. It's fully customizable!</small>
    </div>
    """, unsafe_allow_html=True)
