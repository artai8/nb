import os
from streamlit.components.v1 import html
from nb.config import write_config

package_dir = os.path.dirname(os.path.abspath(__file__))


def get_list(string):
    return [line.strip() for line in string.splitlines() if line.strip()]


def get_string(my_list):
    return "".join(f"{item}\n" for item in my_list)


def dict_to_list(d):
    return [f"{k}: {v}" for k, v in d.items()]


def list_to_dict(my_list):
    my_dict = {}
    for item in my_list:
        k, v = item.split(":")
        my_dict[k.strip()] = v.strip()
    return my_dict


def apply_theme(st, CONFIG, hidden_container):
    if st.session_state.theme == "☀️":
        theme, CONFIG.theme = "Light", "light"
    else:
        theme, CONFIG.theme = "Dark", "dark"
    write_config(CONFIG)
    script = f"<script>localStorage.setItem('stActiveTheme-/-v1', '{{\"name\":\"{theme}\"}}');"
    pages_dir = os.path.join(package_dir, "pages")
    if os.path.isdir(pages_dir):
        for page in os.listdir(pages_dir):
            if page.endswith(".py"):
                script += f"localStorage.setItem('stActiveTheme-/{page[4:-3]}-v1', '{{\"name\":\"{theme}\"}}');"
    script += "parent.location.reload()</script>"
    with hidden_container:
        html(script, height=0, width=0)


def switch_theme(st, CONFIG):
    with st.sidebar:
        _, content, _ = st.columns([0.27, 0.46, 0.27])
        with content:
            st.radio("主题:", ["☀️", "🌒"], horizontal=True, label_visibility="collapsed", index=CONFIG.theme == "dark", on_change=apply_theme, key="theme", args=[st, CONFIG, _])


def hide_st(st):
    if os.getenv("DEV"):
        return
    st.markdown("<style>#MainMenu {visibility: hidden;} footer {visibility: hidden;}</style>", unsafe_allow_html=True)
