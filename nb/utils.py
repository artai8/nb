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



# ==================== 新增：极致美化 CSS (Neumorphism + Glassmorphism) ====================
def inject_custom_css(theme: str = "light"):
    """注入 Neumorphism (新拟物化) + Glassmorphism (毛玻璃) 风格 CSS"""
    
    # 定义主题变量
    if theme == "dark":
        # 深色模式变量
        vars_css = """
        :root {
            --bg-color: #212529;
            --text-color: #f8f9fa;
            --shadow-light: #2c3237;
            --shadow-dark: #16191b;
            --glass-bg: rgba(33, 37, 41, 0.75);
            --glass-border: rgba(255, 255, 255, 0.08);
            --primary-color: #6c5ce7;
            --accent-color: #00cec9;
            --input-bg: #212529;
            --card-radius: 20px;
        }
        """
    else:
        # 浅色模式变量 (默认)
        vars_css = """
        :root {
            --bg-color: #e0e5ec;
            --text-color: #4a5568;
            --shadow-light: #ffffff;
            --shadow-dark: #a3b1c6;
            --glass-bg: rgba(255, 255, 255, 0.65);
            --glass-border: rgba(255, 255, 255, 0.4);
            --primary-color: #6c5ce7;
            --accent-color: #00cec9;
            --input-bg: #e0e5ec;
            --card-radius: 20px;
        }
        """

    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700&display=swap');
        
        {vars_css}

        /* 全局样式重置 */
        .stApp {{
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: 'Nunito', sans-serif;
        }}
        
        h1, h2, h3, h4, h5, h6 {{
            color: var(--text-color) !important;
            font-weight: 700;
            text-shadow: 1px 1px 2px var(--shadow-light), -1px -1px 2px var(--shadow-dark);
        }}

        /* --- Glassmorphism Sidebar (侧边栏毛玻璃) --- */
        section[data-testid="stSidebar"] {{
            background-color: var(--glass-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-right: 1px solid var(--glass-border);
            box-shadow: 5px 0 15px rgba(0,0,0,0.05);
        }}
        
        section[data-testid="stSidebar"] .block-container {{
            padding-top: 2rem;
        }}

        /* --- Neumorphism Buttons (新拟物化按钮) --- */
        .stButton > button {{
            border-radius: 12px;
            background: var(--bg-color);
            box-shadow:  6px 6px 12px var(--shadow-dark),
                        -6px -6px 12px var(--shadow-light);
            border: none;
            color: var(--text-color);
            font-weight: 600;
            transition: all 0.2s ease;
            padding: 0.5rem 1rem;
        }}
        
        .stButton > button:hover {{
            transform: translateY(-2px);
            box-shadow:  8px 8px 16px var(--shadow-dark),
                        -8px -8px 16px var(--shadow-light);
            color: var(--primary-color);
        }}
        
        .stButton > button:active {{
            transform: translateY(1px);
            box-shadow: inset 4px 4px 8px var(--shadow-dark),
                        inset -4px -4px 8px var(--shadow-light);
        }}
        
        /* Primary 按钮特殊处理 */
        .stButton button[kind="primary"] {{
            color: var(--primary-color);
            border: 1px solid rgba(108, 92, 231, 0.1);
        }}

        /* --- Neumorphism Inputs (内嵌阴影输入框) --- */
        .stTextInput input, .stTextArea textarea {{
            background-color: var(--input-bg) !important;
            border-radius: 12px;
            border: none;
            box-shadow: inset 5px 5px 10px var(--shadow-dark),
                        inset -5px -5px 10px var(--shadow-light);
            color: var(--text-color);
            padding: 10px 12px;
        }}

        .stSelectbox div[data-baseweb="select"] > div {{
            background-color: var(--input-bg) !important;
            border-radius: 12px;
            border: none;
            box-shadow: inset 5px 5px 10px var(--shadow-dark),
                        inset -5px -5px 10px var(--shadow-light);
            color: var(--text-color);
            padding: 5px 12px;
            min-height: 45px;
            display: flex;
            align-items: center;
            position: relative;
        }}
        
        /* 修复选中值被遮挡的问题 */
        .stSelectbox div[data-baseweb="select"] > div > div {{
            z-index: 1;
        }}
        
        .stTextInput input:focus, .stTextArea textarea:focus {{
            outline: none;
            box-shadow: inset 2px 2px 5px var(--shadow-dark),
                        inset -2px -2px 5px var(--shadow-light),
                        0 0 5px var(--primary-color);
        }}

        /* --- Glassmorphism Cards / Expanders (卡片/折叠框) --- */
        div[data-testid="stExpander"] {{
            background: var(--bg-color);
            border-radius: var(--card-radius);
            border: 1px solid var(--glass-border);
            box-shadow:  9px 9px 16px var(--shadow-dark),
                        -9px -9px 16px var(--shadow-light);
            margin-bottom: 1rem;
            overflow: hidden;
        }}
        
        .streamlit-expanderHeader {{
            background-color: transparent !important;
            color: var(--text-color) !important;
            font-weight: 600;
            border-bottom: 1px solid var(--glass-border);
        }}
        
        div[data-testid="stExpander"] > div:last-child {{
            padding: 1rem;
        }}

        /* --- Tabs (标签页) --- */
        .stTabs [data-baseweb="tab-list"] {{
            gap: 16px;
            background-color: transparent;
            padding-bottom: 10px;
        }}
        
        .stTabs [data-baseweb="tab"] {{
            height: 45px;
            border-radius: 12px;
            background-color: var(--bg-color);
            box-shadow:  5px 5px 10px var(--shadow-dark),
                        -5px -5px 10px var(--shadow-light);
            color: var(--text-color);
            border: none;
            padding: 0 20px;
        }}
        
        .stTabs [aria-selected="true"] {{
            color: var(--primary-color);
            box-shadow: inset 3px 3px 6px var(--shadow-dark),
                        inset -3px -3px 6px var(--shadow-light);
        }}

        /* --- Checkbox & Radio (开关与单选) --- */
        /* 自定义 Checkbox 较难完全覆盖，尝试用容器包裹 */
        div[data-baseweb="checkbox"] {{
            margin-bottom: 0.5rem;
        }}
        
        /* Alert / Info Boxes (提示框) */
        .stAlert {{
            background-color: var(--bg-color);
            border-radius: 12px;
            box-shadow: inset 3px 3px 6px var(--shadow-dark),
                        inset -3px -3px 6px var(--shadow-light);
            border: none;
            color: var(--text-color);
        }}
        
        /* Code Block */
        .stCodeBlock {{
            border-radius: 12px;
            box-shadow: inset 3px 3px 6px var(--shadow-dark),
                        inset -3px -3px 6px var(--shadow-light);
        }}
        
        /* 隐藏顶部红条和页脚 */
        header[data-testid="stHeader"] {{
            background: transparent;
        }}
        footer {{visibility: hidden;}}

        /* --- Custom Utility Classes (自定义工具类) --- */
        .neu-card {{
            background-color: var(--bg-color);
            border-radius: var(--card-radius);
            box-shadow:  9px 9px 16px var(--shadow-dark),
                        -9px -9px 16px var(--shadow-light);
            padding: 20px;
            border: 1px solid var(--glass-border);
            height: 100%;
        }}
        
        .glass-card {{
            background: var(--glass-bg);
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            border-radius: var(--card-radius);
            border: 1px solid var(--glass-border);
            padding: 20px;
        }}
        
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
    theme_val = "light"
    if CONFIG.theme == "dark":
        theme_val = "dark"
    inject_custom_css(theme=theme_val)
    
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
