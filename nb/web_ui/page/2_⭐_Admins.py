import streamlit as st

from nb.config import CONFIG, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import get_list, get_string, hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Admins",
    page_icon="⭐",
)
hide_st(st)
switch_theme(st,CONFIG)
if check_password(st):

    CONFIG.admins = get_list(st.text_area("Admins", value=get_string(CONFIG.admins)))
    st.write("Add the usernames of admins. One in each line.")

    if st.button("Save"):
        write_config(CONFIG)
