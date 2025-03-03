import streamlit as st
st.set_page_config(page_title="EFI Portfolio Tracker", layout="wide")
    

import subprocess
import sys

@st.cache_resource()
def install_latest_yfinance():
    """Ensure the latest version of yfinance is installed."""
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yfinance"], check=True)

# Run the update before importing yfinance
install_latest_yfinance()

import time
import cloudscraper
import random
import pandas as pd
import datetime
import os
from supabase import create_client, Client
import yfinance as yf
from zoneinfo import ZoneInfo
import altair as alt

# -------------------------------------------------------------------------
# 1) Supabase / Environment Setup
# -------------------------------------------------------------------------
url = st.secrets["SUPABASE_URL"]
key = st.secrets["SUPABASE_KEY"]
supabase: Client = create_client(url, key)

apiUrl = st.secrets["API"]
baseURL = st.secrets["BASEAPI"]

# -------------------------------------------------------------------------
# 2) Option Chain Fetch & Mid-Price Logic
# -------------------------------------------------------------------------
@st.cache_data(ttl=60*60)
def get_options_chain(symbol: str):
    time.sleep(1)  # simulate some network delay
    full_url = f"{baseURL}?stock={symbol.upper()}&reqId={random.randint(1, 1000000)}"
    scraper = cloudscraper.create_scraper()
    response = scraper.get(full_url)
    if response.status_code == 200:
        return response.json()
    else:
        st.error(f"❌ Failed to fetch options chain for {symbol}. Status code: {response.status_code}")
        return None

def fetch_option_price(symbol: str, expiration: str, strike: float, call_put: str) -> float:
    data = get_options_chain(symbol)
    if not data or "options" not in data:
        raise ValueError("Option chain data not found or invalid JSON structure.")

    all_opts = data["options"]
    if expiration not in all_opts:
        raise ValueError(f"No expiration {expiration} found in chain for {symbol}.")

    cp_key = "c" if call_put.upper() == "CALL" else "p"
    cp_dict = all_opts[expiration].get(cp_key, {})
    if not cp_dict:
        raise ValueError(f"No {call_put} data found for expiration {expiration} in chain.")

    strike_key = f"{strike:.2f}"
    if strike_key not in cp_dict:
        raise ValueError(f"Strike {strike} not found for {call_put} {expiration} {symbol}.")

    option_data = cp_dict[strike_key]
    bid = option_data.get("b", 0)
    ask = option_data.get("a", 0)
    if ask <= 0:
        raise ValueError(f"Ask price invalid or zero for {call_put} {expiration} {symbol} strike {strike}.")

    mid_price = (bid + ask) / 2
    return mid_price

# -------------------------------------------------------------------------
# 3) Fetch Current Prices (Shares)
# -------------------------------------------------------------------------
def fetch_share_price(ticker: str) -> float:
    try:
        data = yf.Ticker(ticker).history(period="1d")
        if len(data) > 0:
            return float(data["Close"].iloc[-1])
    except:
        st.error(f'❌ Failed Fetching {ticker} Price')
    return 0.0

# -------------------------------------------------------------------------
# 4) Database CRUD Helpers
# -------------------------------------------------------------------------
def load_settings() -> pd.DataFrame:
    resp = supabase.table("settings").select("*").eq("id", 1).execute()
    data = resp.data
    return pd.DataFrame(data) if data else pd.DataFrame()

def save_settings(original_capital: float):
    supabase.table("settings").upsert({"id": 1, "original_capital": original_capital}, on_conflict="id").execute()
    st.rerun()

# ---- SHARES ----
def load_shares() -> pd.DataFrame:
    resp = supabase.table("portfolio_shares").select("*").execute()
    data = resp.data
    return pd.DataFrame(data) if data else pd.DataFrame()

def upsert_share(ticker: str, shares_held: float, avg_cost: float, current_price: float):
    unreal_pl = (current_price - avg_cost) * shares_held
    supabase.table("portfolio_shares").upsert({
        "ticker": ticker,
        "shares_held": shares_held,
        "avg_cost": avg_cost,
        "current_price": current_price,
        "unrealized_pl": unreal_pl
    }, on_conflict="ticker").execute()

def delete_share(ticker: str):
    supabase.table("portfolio_shares").delete().eq("ticker", ticker).execute()

# ---- OPTIONS ----
def load_options() -> pd.DataFrame:
    resp = supabase.table("portfolio_options").select("*").execute()
    data = resp.data
    return pd.DataFrame(data) if data else pd.DataFrame()

def upsert_option(opt_id: int, symbol: str, call_put: str, expiration: str, strike: float,
                  contracts_held: float, avg_cost: float, current_price: float):
    unreal_pl = (current_price - avg_cost) * (contracts_held * 100)
    data_dict = {
        "symbol": symbol,
        "call_put": call_put,
        "expiration": expiration,
        "strike": strike,
        "contracts_held": contracts_held,
        "avg_cost": avg_cost,
        "current_price": current_price,
        "unrealized_pl": unreal_pl
    }
    if opt_id is None:
        supabase.table("portfolio_options").insert(data_dict).execute()
    else:
        supabase.table("portfolio_options").update(data_dict).eq("id", opt_id).execute()

def delete_option(row_id: int):
    supabase.table("portfolio_options").delete().eq("id", row_id).execute()

# ---- PERFORMANCE ----
def load_performance() -> pd.DataFrame:
    resp = supabase.table("performance").select("*").execute()
    data = resp.data
    return pd.DataFrame(data) if data else pd.DataFrame()

def upsert_performance(date_str: str, total_value: float):
    supabase.table("performance").upsert({"date": date_str, "total_value": total_value}, on_conflict="date").execute()

# ---- ACTIVITY LOG ----
def load_activity() -> pd.DataFrame:
    resp = (
        supabase.table("portfolio_activity")
        .select("*")
        .order("id", desc=True)
        .limit(15)
        .execute()
    )
    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()

def log_activity(message: str):
    supabase.table("portfolio_activity").insert({"message": message}).execute()

# -------------------------------------------------------------------------
# 5) Automatic Refresh (Once Per Session)
# -------------------------------------------------------------------------
def refresh_shares_prices():
    shares_df = load_shares()
    if shares_df.empty:
        return
    for idx, row in shares_df.iterrows():
        ticker = row["ticker"]
        shares_held = float(row["shares_held"])
        avg_cost = float(row["avg_cost"])
        current_px = fetch_share_price(ticker)
        unreal_pl = (current_px - avg_cost) * shares_held

        supabase.table("portfolio_shares").upsert({
            "ticker": ticker,
            "shares_held": shares_held,
            "avg_cost": avg_cost,
            "current_price": current_px,
            "unrealized_pl": unreal_pl
        }, on_conflict="ticker").execute()

def refresh_options_prices():
    opt_df = load_options()
    if opt_df.empty:
        return
    for idx, row in opt_df.iterrows():
        opt_id = row["id"]
        symbol = row["symbol"]
        call_put = row["call_put"]
        expiration_dt = row["expiration"]
        expiration_str = expiration_dt.strftime("%Y-%m-%d") if not isinstance(expiration_dt, str) else expiration_dt

        strike = float(row["strike"])
        contracts_held = float(row["contracts_held"])
        avg_cost = float(row["avg_cost"])

        current_px = fetch_option_price(symbol, expiration_str, strike, call_put)
        unreal_pl = (current_px - avg_cost) * contracts_held * 100

        supabase.table("portfolio_options").update({
            "symbol": symbol,
            "call_put": call_put,
            "expiration": expiration_str,
            "strike": strike,
            "contracts_held": contracts_held,
            "avg_cost": avg_cost,
            "current_price": current_px,
            "unrealized_pl": unreal_pl
        }).eq("id", opt_id).execute()

def refresh():
    time.sleep(1)
    st.rerun()

def record_daily_performance():
    """
    Sums up:
      - total value in shares
      - total value in options
      - plus unused capital (buying power)
    Then upserts into the 'performance' table using today's date.
    """
    shares_df = load_shares()
    total_shares_val = (shares_df["shares_held"] * shares_df["current_price"]).sum() if not shares_df.empty else 0.0

    opt_df = load_options()
    total_opts_val = ((opt_df["contracts_held"] * 100) * opt_df["current_price"]).sum() if not opt_df.empty else 0.0

    settings_df = load_settings()
    if not settings_df.empty:
        original_cap_def = float(settings_df.iloc[0]["original_capital"])
    else:
        original_cap_def = 0.0

    spent_shares_val = (shares_df["shares_held"] * shares_df["avg_cost"]).sum() if not shares_df.empty else 0.0
    spent_opts_val = ((opt_df["contracts_held"] * 100) * opt_df["avg_cost"]).sum() if not opt_df.empty else 0.0
    buying_power = original_cap_def - spent_shares_val - spent_opts_val
    total_val = float(total_shares_val + total_opts_val + buying_power)

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    upsert_performance(today_str, total_val)

def refresh_all_once():
    if "did_refresh" not in st.session_state:
        st.session_state["did_refresh"] = False

    if not st.session_state["did_refresh"]:
        with st.spinner('Fetching Position Values...'):
            refresh_shares_prices()
            refresh_options_prices()
            record_daily_performance()
            st.session_state["did_refresh"] = True

# -------------------------------------------------------------------------
# 6) Helper for Color-coding Unrealized P/L cells
# -------------------------------------------------------------------------
def color_unreal_pl(val):
    if val > 0:
        return "color: #65FE08"
    elif val < 0:
        return "color: red"
    else:
        return ""

# -------------------------------------------------------------------------
# 7) Logging Activity: shares or options
# -------------------------------------------------------------------------
def get_est_time():
    now_est = datetime.datetime.now(ZoneInfo("America/New_York"))
    return now_est.strftime("%m/%d/%Y %I:%M%p")

def log_shares_activity(ticker: str, shares_added: float, price: float):
    action = "BOUGHT" if shares_added > 0 else "SOLD"
    color = "#65FE08" if shares_added > 0 else "red"
    sign = "+" if shares_added > 0 else ""
    cost = price * shares_added
    now_str = get_est_time()

    msg = (
        f"<b style='color:{color};'>{action} {sign}{shares_added} shares</b> "
        f"of <b style='color:#FFD700;'>{ticker}</b> "
        f"at <b>/${price:,.2f}</b> "
        f"(Total: <b style='color:{color};'>{sign}/${abs(cost):,.2f}</b>) "
        f"on {now_str}"
    )
    log_activity(msg)

def log_options_activity(opt_id, symbol, call_put, expiration, strike, contracts_added, price):
    action = "BOUGHT" if contracts_added > 0 else "SOLD"
    color = "#65FE08" if contracts_added > 0 else "red"
    sign = "+" if contracts_added > 0 else ""
    total_cost = price * contracts_added * 100
    now_str = get_est_time()
    exp_str = expiration if isinstance(expiration, str) else expiration.strftime("%Y-%m-%d")

    msg = (
        f"<b style='color:{color};'>{action} {sign}{contracts_added} contract(s)</b> of "
        f"<b style='color:#FFD700;'> {symbol} {strike:.2f} {call_put} {exp_str}</b> "
        f"at <b>/${price:,.2f}</b> "
        f"(Total: <b style='color:{color};'>{sign}/${abs(total_cost):,.2f}</b>) "
        f"on {now_str}"
    )
    log_activity(msg)

# -------------------------------------------------------------------------
# 8) Main App with Password Gate
# -------------------------------------------------------------------------
def main():

    # Initialize 'is_admin' in session state
    if "is_admin" not in st.session_state:
        st.session_state["is_admin"] = False
    
    # If user is NOT admin, prompt for password & show view-only
    if not st.session_state["is_admin"]:
        st.title("EFI Portfolio Tracker ⚡ (View-Only)")
        
        # Ask for admin password to unlock editing
        input_password = st.text_input("Enter admin password to edit:", type="password")
        if st.button("Login"):
            if input_password == st.secrets["ADMIN_PASSWORD"]:  # <-- Must add 'ADMIN_PASSWORD' to your Streamlit secrets
                st.session_state["is_admin"] = True
                st.rerun()
            else:
                st.error("Wrong password!")
        
        # We still want to run refresh and show the data, but no editing
        refresh_all_once()
        show_portfolio_data(is_admin=False)  # Custom helper below
    else:
        # Admin mode
        st.title("EFI Portfolio Tracker ⚡ (Admin)")
        
        if st.button("Logout"):
            st.session_state["is_admin"] = False
            st.rerun()
        
        # Run refresh and show full UI
        refresh_all_once()
        show_portfolio_data(is_admin=True)

def show_portfolio_data(is_admin: bool):
    """
    Show the entire portfolio data, with or without editing widgets
    based on is_admin boolean.
    """
    # 1) Load settings
    settings_df = load_settings()
    if not settings_df.empty:
        original_cap_def = float(settings_df.iloc[0]["original_capital"])
    else:
        original_cap_def = 0.0

    # 2) Basic account stats
    shares_df = load_shares()
    total_shares_val = (shares_df["shares_held"] * shares_df["current_price"]).sum() if not shares_df.empty else 0.0

    opt_df = load_options()
    total_opts_val = ((opt_df["contracts_held"] * 100) * opt_df["current_price"]).sum() if not opt_df.empty else 0.0

    spent_shares_val = (shares_df["shares_held"] * shares_df["avg_cost"]).sum() if not shares_df.empty else 0.0
    spent_opts_val = ((opt_df["contracts_held"] * 100) * opt_df["avg_cost"]).sum() if not opt_df.empty else 0.0

    buying_power = original_cap_def - spent_shares_val - spent_opts_val
    total_account_val = float(total_shares_val + total_opts_val + buying_power)
    percent_bp = (buying_power / total_account_val * 100) if total_account_val != 0 else 0

    colA = st.columns(5)
    colA[0].number_input(
        "Original Starting Capital ($)",
        value=original_cap_def,
        step=1000.0,
        key="orig_capital",
        disabled=(not is_admin)
    )
    # Calculate percentages for shares and options
    shares_percentage = (total_shares_val / total_account_val * 100) if total_account_val > 0 else 0
    options_percentage = (total_opts_val / total_account_val * 100) if total_account_val > 0 else 0

    # Display Total Account and portions with percentages
    colA[1].number_input(
        "Total Account ($)", 
        value=total_account_val, 
        step=500.0, 
        disabled=True
    )
    colA[2].number_input(
        f"Shares Portion ($) - {shares_percentage:.2f}%", 
        value=total_shares_val, 
        step=500.0, 
        disabled=True
    )
    colA[3].number_input(
        f"Options Portion ($) - {options_percentage:.2f}%", 
        value=total_opts_val, 
        step=500.0, 
        disabled=True
    )
    colA[4].number_input(
        f"Buying Power ($) - {percent_bp:.2f}%",
        value=buying_power,
        step=500.0,
        disabled=True
    )

    # Only show "Save Original Capital" button if admin
    if is_admin:
        if st.button("💾 Save Original Capital"):
            save_settings(st.session_state["orig_capital"])
            st.success("💾 Saved Original Capital!")

    st.write("---")

    # Activity Log Expander
    with st.expander("Recent Activity Log"):
        activity_df = load_activity()
        if activity_df.empty:
            st.write("No activity yet.")
        else:
            for idx, row in activity_df.iterrows():
                st.markdown(f"• {row['message']}", unsafe_allow_html=True)

    st.write("---")

    # TABS
    tab_shares, tab_opts, tab_perf = st.tabs(["📈 Shares", "🧩 Options", "📊 Performance"])

    # --------------------- SHARES TAB ---------------------
    with tab_shares:
        st.markdown("## Shares Portfolio 🚀")
        shares_df = load_shares()
        if shares_df.empty:
            st.info("No shares in portfolio yet. Add some below! 🌱" if is_admin else "No shares in portfolio yet.")
        else:
            df_disp = shares_df.copy()
            df_disp["Position Value"] = df_disp["shares_held"] * df_disp["current_price"]
            df_disp["Currently Invested"] = df_disp["shares_held"] * df_disp["avg_cost"]
            if total_account_val > 0:
                df_disp["% of Portfolio"] = (df_disp["Position Value"] / total_account_val) * 100
            else:
                df_disp["% of Portfolio"] = 0

            df_disp = df_disp[[
                "ticker", "shares_held", "avg_cost", "current_price",
                "Currently Invested", "Position Value", "unrealized_pl", "% of Portfolio"
            ]]

            df_disp = df_disp.rename(columns={
                "ticker": "Ticker",
                "shares_held": "Shares",
                "avg_cost": "Avg Cost",
                "current_price": "Current Price",
                "unrealized_pl": "Unrealized P/L"
            })

            def money(x): return f"${x:,.2f}"
            def percent(x): return f"{x:,.2f}%"

            styled_shares = (
                df_disp.style
                .format({
                    "Shares": "{:.2f}",
                    "Avg Cost": money,
                    "Current Price": money,
                    "Currently Invested": money,
                    "Position Value": money,
                    "Unrealized P/L": money,
                    "% of Portfolio": percent
                })
                .map(color_unreal_pl, subset=["Unrealized P/L"])
            )
            row_height = 40  
            num_rows = len(df_disp)
            dynamic_height = max(250, num_rows * row_height)
            st.dataframe(styled_shares, use_container_width=True, height=dynamic_height)

        # Show editing form only if admin
        if is_admin:
            st.subheader("Add / Update Shares 🏗️")
            tickers_list = shares_df["ticker"].tolist() if not shares_df.empty else []
            sel_share = st.selectbox("Select existing Ticker or create new", tickers_list + ["(New)"])

            if sel_share == "(New)":
                new_ticker = st.text_input("New Ticker Symbol (e.g. AAPL)", key="new_ticker_shares")
                ticker_val = new_ticker.upper()
                old_shares, old_avg = 0.0, 0.0
            else:
                ticker_val = sel_share
                existing_row = shares_df[shares_df["ticker"] == ticker_val]
                if not existing_row.empty:
                    old_shares = float(existing_row["shares_held"].iloc[0])
                    old_avg = float(existing_row["avg_cost"].iloc[0])
                else:
                    old_shares, old_avg = 0.0, 0.0

            shares_to_add = st.number_input("Shares to Add (negative to reduce)", step=1.0)
            purchase_price = st.number_input(
                "Filled Price per share",
                value=fetch_share_price(ticker_val) if ticker_val else 0.0,
                step=1.0
            )

            if st.button("Submit (Shares)"):
                total_shares = old_shares + shares_to_add
                if total_shares < 0:
                    st.error("Cannot have negative total shares.")
                    st.stop()
                elif total_shares == 0:
                    delete_share(ticker_val)
                    st.warning(f"Position closed for {ticker_val}.")
                    # Log the activity
                    if shares_to_add != 0:
                        log_shares_activity(ticker_val, shares_to_add, purchase_price)
                    refresh()
                else:
                    new_avg = 0.0
                    if (old_shares + shares_to_add) != 0:
                        new_avg = (old_shares * old_avg + shares_to_add * purchase_price) / (old_shares + shares_to_add)

                    current_px = fetch_share_price(ticker_val)
                    upsert_share(ticker_val, total_shares, new_avg, current_px)

                    if shares_to_add != 0:
                        log_shares_activity(ticker_val, shares_to_add, purchase_price)
                    st.success(f"✅ Updated {ticker_val} with total shares={total_shares:.2f}, avg_cost={new_avg:.2f}")
                    refresh()

            st.subheader("Delete Entire Share Position 🗑️")
            del_ticker_sh = st.selectbox("Select Ticker to Delete Entirely", ["(None)"] + tickers_list)
            if del_ticker_sh != "(None)":
                if st.button("Confirm Delete (Shares)"):
                    delete_share(del_ticker_sh)
                    st.warning(f"🗑️ Deleted entire {del_ticker_sh} share position.")
                    refresh()

    # --------------------- OPTIONS TAB ---------------------
    with tab_opts:
        st.markdown("## Options Portfolio 🔧")
        opt_df = load_options()
        if opt_df.empty:
            st.info("No options in portfolio yet." if not is_admin else "No options in portfolio yet. Add some below! 🤔")
        else:
            df_o = opt_df.copy()
            df_o["Position Value"] = df_o["contracts_held"] * 100 * df_o["current_price"]
            df_o["Currently Invested"] = df_o["contracts_held"] * 100 * df_o["avg_cost"]
            if total_account_val > 0:
                df_o["% of Portfolio"] = (df_o["Position Value"] / total_account_val) * 100
            else:
                df_o["% of Portfolio"] = 0

            df_o = df_o[[
                "symbol", "call_put", "expiration", "strike", "contracts_held",
                "avg_cost", "current_price", "Currently Invested", "Position Value",
                "unrealized_pl", "% of Portfolio"
            ]]

            df_o = df_o.rename(columns={
                "symbol": "Symbol",
                "call_put": "Call/Put",
                "expiration": "Expiration",
                "strike": "Strike",
                "contracts_held": "Contracts",
                "avg_cost": "Avg Cost",
                "current_price": "Current Price",
                "unrealized_pl": "Unrealized P/L"
            })

            def money(x): return f"${x:,.2f}"
            def percent(x): return f"{x:,.2f}%"

            styled_opts = (
                df_o.style
                .format({
                    "Strike": money,
                    "Contracts": "{:.2f}",
                    "Avg Cost": money,
                    "Current Price": money,
                    "Currently Invested": money,
                    "Position Value": money,
                    "Unrealized P/L": money,
                    "% of Portfolio": percent
                })
                .map(color_unreal_pl, subset=["Unrealized P/L"])
            )
            row_height = 40
            num_rows = len(df_o)
            dynamic_height = max(250, num_rows * row_height)

            st.dataframe(styled_opts, use_container_width=True, height=dynamic_height)

        # Show editing only if admin
        if is_admin:
            st.subheader("Add / Update an Option 🔧")
            existing_opts = []
            if not opt_df.empty:
                for _, ro in opt_df.iterrows():
                    row_label = f"{ro['id']}: {ro['symbol']} {ro['call_put']} {ro['strike']} exp={ro['expiration']}"
                    existing_opts.append(row_label)

            chosen_opt = st.selectbox("Select existing Option or (New)", existing_opts + ["(New)"])
            if chosen_opt == "(New)":
                opt_id = None
                symbol_input = st.text_input("Option Symbol (e.g. SPY)")
                call_put_input = st.selectbox("CALL or PUT", ["CALL", "PUT"])
                exp_in = st.date_input("Expiration Date")
                strike_in = st.number_input("Strike", step=1.0)
                old_contracts = 0.0
                old_avg = 0.0
            else:
                row_id = int(chosen_opt.split(":")[0])
                row_data = opt_df[opt_df["id"] == row_id].squeeze()
                opt_id = row_id
                symbol_input = row_data["symbol"]
                call_put_input = row_data["call_put"]
                exp_in = row_data["expiration"]
                strike_in = float(row_data["strike"])
                old_contracts = float(row_data["contracts_held"])
                old_avg = float(row_data["avg_cost"])

            contracts_to_add = st.number_input("Contracts to Add (negative to reduce)", step=1.0)
            purchase_price_opt = st.number_input("Filled Price (per contract)", step=1.0)

            if isinstance(exp_in, datetime.date):
                exp_str = exp_in.strftime("%Y-%m-%d")
            else:
                exp_str = str(exp_in)

            if st.button("Submit (Options)"):
                total_contracts = old_contracts + contracts_to_add
                if total_contracts < 0:
                    st.error("Cannot have negative total contracts.")
                    st.stop()
                elif total_contracts == 0:
                    if opt_id is not None:
                        delete_option(opt_id)
                        st.warning("Option closed out entirely. 🗑️")
                        if contracts_to_add != 0:
                            log_options_activity(
                                opt_id, symbol_input, call_put_input, exp_str, strike_in,
                                contracts_to_add, purchase_price_opt
                            )
                        refresh()
                else:
                    new_avg_opt = 0.0
                    if old_contracts + contracts_to_add != 0:
                        new_avg_opt = (
                            (old_contracts * old_avg) + (contracts_to_add * purchase_price_opt)
                        ) / (old_contracts + contracts_to_add)

                    current_opt_px = fetch_option_price(symbol_input, exp_str, strike_in, call_put_input)
                    upsert_option(
                        opt_id,
                        symbol_input,
                        call_put_input,
                        exp_str,
                        strike_in,
                        total_contracts,
                        new_avg_opt,
                        current_opt_px
                    )
                    if contracts_to_add != 0:
                        log_options_activity(
                            opt_id, symbol_input, call_put_input, exp_str, strike_in,
                            contracts_to_add, purchase_price_opt
                        )
                    st.success(
                        f"✅ Updated Option: {symbol_input} {call_put_input}, "
                        f"total_contracts={total_contracts:.2f}, avg={new_avg_opt:.2f}"
                    )
                    refresh()

            st.subheader("Delete an Option Entirely 🗑️")
            del_opt_sel = st.selectbox("Select Option to Delete", ["(None)"] + existing_opts)
            if del_opt_sel != "(None)":
                if st.button("Confirm Delete (Option)"):
                    del_id = int(del_opt_sel.split(":")[0])
                    delete_option(del_id)
                    st.warning(f"🗑️ Deleted option ID {del_id}.")
                    refresh()

    # --------------------- PERFORMANCE TAB ---------------------
    with tab_perf:
        st.markdown("## Performance History 📊")
        perf_df = load_performance()
        if not perf_df.empty:
            perf_df = perf_df.sort_values("date")
            perf_df.rename(columns={'date': 'Date', 'total_value': 'Portfolio Value'}, inplace=True)
            chart = (
                alt.Chart(perf_df)
                .mark_line()
                .encode(
                    x="Date:T",
                    y=alt.Y("Portfolio Value:Q", scale=alt.Scale(zero=False)),  # Ensure y-axis dynamically scales
                )
                .properties(height=300, width="container")
            )

            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("No performance records yet. 📉")

if __name__ == "__main__":
    main()
