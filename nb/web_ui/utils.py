# nb/web_ui/utils.py

import os
from typing import Dict, List

import streamlit as st
from streamlit.components.v1 import html
from nb.config import write_config


def _get_package_dir() -> str:
    """获取 web_ui 包的实际文件系统路径"""
    return os.path.dirname(os.path.abspath(__file__))


package_dir = _get_package_dir()


def get_list(string: str):
    my_list = []
    for line in string.splitlines():
        clean_line = line.strip()
        if clean_line != "":
            my_list.append(clean_line)
    return my_list


def get_string(my_list: List):
    string = ""
    for item in my_list:
        string += f"{item}\n"
    return string


def dict_to_list(dict: Dict):
    my_list = []
    for key, val in dict.items():
        my_list.append(f"{key}: {val}")
    return my_list


def list_to_dict(my_list: List):
    my_dict = {}
    for item in my_list:
        key, val = item.split(":")
        my_dict[key.strip()] = val.strip()
    return my_dict


# ==================== 新增：极致美化 CSS ====================
def inject_custom_css():
    """注入 SaaS Dashboard 风格 CSS"""
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap');
        
        /* 全局变量 */
        :root {
            --primary: #6366f1; /* Indigo */
            --primary-hover: #4f46e5;
            --bg-body: #f8fafc;
            --text-main: #1e293b;
            --radius: 10px;
        }

        /* 基础重置 */
        .stApp {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-body);
        }
        
        h1, h2, h3 { font-weight: 700 !important; letter-spacing: -0.5px; color: #0f172a; }
        
        /* 侧边栏美化 */
        section[data-testid="stSidebar"] {
            background-color: #0f172a; /* 深色侧边栏 */
            color: #f8fafc;
        }
        /* 侧边栏文字颜色覆盖 */
        section[data-testid="stSidebar"] h1, 
        section[data-testid="stSidebar"] h2, 
        section[data-testid="stSidebar"] h3, 
        section[data-testid="stSidebar"] span, 
        section[data-testid="stSidebar"] label,
        section[data-testid="stSidebar"] p {
            color: #cbd5e1 !important; 
        }
        
        /* 按钮重绘 */
        .stButton button {
            border-radius: 8px;
            font-weight: 600;
            border: 1px solid #e2e8f0;
            transition: all 0.2s;
            box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }
        .stButton button:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }
        
        /* Primary 按钮 (紫色渐变) */
        .stButton button[kind="primary"] {
            background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
            border: none;
            color: white;
            box-shadow: 0 4px 6px -1px rgba(79, 70, 229, 0.3);
        }
        .stButton button[kind="primary"]:hover {
            background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%);
            box-shadow: 0 6px 10px -1px rgba(79, 70, 229, 0.4);
        }

        /* Secondary 按钮 (红色用于删除/停止) */
        .stButton button[kind="secondary"] {
            background-color: white;
            color: #ef4444;
            border-color: #fee2e2;
        }
        .stButton button[kind="secondary"]:hover {
            background-color: #fef2f2;
            border-color: #fca5a5;
        }

        /* 输入框优化 */
        .stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] {
            border-radius: 8px;
            border: 1px solid #cbd5e1;
            background-color: #ffffff;
            color: #334155;
        }
        .stTextInput input:focus, .stTextArea textarea:focus {
            border-color: #6366f1;
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
        }

        /* 卡片化容器 (Expander) */
        .streamlit-expanderHeader {
            background-color: white;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            color: #334155;
            font-weight: 600;
        }
        div[data-testid="stExpander"] {
            background-color: white;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.05);
            overflow: hidden;
        }
        div[data-testid="stExpander"] > div:first-child {
             border-bottom: 1px solid #f1f5f9;
        }

        /* Tab 样式 */
        .stTabs [data-baseweb="tab-list"] {
            gap: 8px;
            background-color: transparent;
            border-bottom: 2px solid #e2e8f0;
        }
        .stTabs [data-baseweb="tab"] {
            height: 40px;
            border-radius: 6px 6px 0 0;
            padding: 0 16px;
            color: #64748b;
            font-weight: 500;
        }
        .stTabs [aria-selected="true"] {
            color: #6366f1;
            background-color: white;
            border-bottom: 2px solid #6366f1;
            margin-bottom: -2px;
        }

        /* 状态框 Badge */
        div[data-testid="stMarkdownContainer"] p {
            line-height: 1.6;
        }

        /* 隐藏顶部红条 */
        header[data-testid="stHeader"] {
            background: transparent;
        }
        
        /* 隐藏 Footer */
        footer {visibility: hidden;}
        
        /* 告警框优化 */
        .stAlert {
            border-radius: 8px;
            border: none;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

def apply_theme(st, CONFIG, hidden_container):
    """Apply theme using browser's local storage"""
    if st.session_state.theme == "☀️":
        theme = "Light"
        CONFIG.theme = "light"
    else:
        theme = "Dark"
        CONFIG.theme = "dark"
    write_config(CONFIG)

    script = f"<script>localStorage.setItem('stActiveTheme-/-v1', '{{\"name\":\"{theme}\"}}');"

    pages_dir = os.path.join(package_dir, "pages")
    if os.path.isdir(pages_dir):
        pages = os.listdir(pages_dir)
        for page in pages:
            if page.endswith(".py"):
                page_name = page[4:-3]
                script += f"localStorage.setItem('stActiveTheme-/{page_name}-v1', '{{\"name\":\"{theme}\"}}');"

    script += "parent.location.reload()</script>"
    with hidden_container:
        html(script, height=0, width=0)


def switch_theme(st, CONFIG):
    """Display the option to change theme (Light/Dark)"""
    # ★★★ 关键：在这里调用 CSS 注入 ★★★
    inject_custom_css()
    
    with st.sidebar:
        st.markdown("---")
        leftpad, content, rightpad = st.columns([0.1, 0.8, 0.1])
        with content:
            st.caption("Theme Mode")
            st.radio(
                "Theme:",
                ["☀️", "🌒"],
                horizontal=True,
                label_visibility="collapsed",
                index=CONFIG.theme == "dark",
                on_change=apply_theme,
                key="theme",
                args=[st, CONFIG, leftpad],
            )


def hide_st(st):
    # 已在 inject_custom_css 中处理，这里保留用于兼容
    pass
