from flask import Flask, request, render_template
import requests
from bs4 import BeautifulSoup as bs
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import urllib.parse
import time
import re

app = Flask(__name__)

@app.route("/", methods=['GET'])
def home():
    """Render the home page with the product search form."""
    return render_template('index.html')

@app.route("/search-suggestions")
def search_suggestions():
    """Return search suggestions from Flipkart autocomplete API."""
    query = request.args.get('q', '')
    if not query:
        return {"suggestions": []}
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        url = f"https://www.flipkart.com/api/5/page/fetch?q={urllib.parse.quote(query)}&as=on&as-show=on&otracker=AS_Query_OrganicAutoSuggest"
        resp = requests.get(url, headers=headers, timeout=5)
        suggestions = []
        if resp.status_code == 200:
            data = resp.json()
            items = data.get('RESPONSE', {}).get('data', {}).get('4', {}).get('data', {}).get('products', [])
            for item in items[:8]:
                title = item.get('logicOutputData', {}).get('searchKeyword', '')
                if title:
                    suggestions.append(title)
        return {"suggestions": suggestions}
    except Exception:
        return {"suggestions": []}


def parse_reviews_from_html(product_html):
    """
    Robust, multi-strategy review extraction from Flipkart product HTML.
    Returns a list of review dicts.
    """
    comment_boxes = []
    seen = set()

    # --- Strategy 1: Standard class-based container selection ---
    known_classes = ["RcXBOT", "_27M-vq", "col _2wzgFH", "col EPCmJX", "col _2wzgFH K0kLPL", "EKr-tv", "css-g5y9jx"]
    for cls in known_classes:
        boxes = product_html.find_all("div", class_=cls)
        for box in boxes:
            # For css-g5y9jx, only keep if it looks like a review card (contains Certified Buyer or a rating)
            if cls == "css-g5y9jx":
                if "Certified Buyer" not in box.get_text():
                    continue
            if id(box) not in seen:
                seen.add(id(box))
                comment_boxes.append(box)

    # --- Strategy 2: Find review text divs and walk up to the container ---
    if not comment_boxes:
        text_elements = product_html.find_all("div", class_=re.compile(r't-ZTKy|ZmyHeo'))
        for text_elem in text_elements:
            parent = text_elem.parent
            for _ in range(6):
                if parent and parent.name == 'div':
                    if parent.find(['p', 'span'], class_=re.compile(r'_2NsDsF|Certified Buyer')):
                        if id(parent) not in seen:
                            seen.add(id(parent))
                            comment_boxes.append(parent)
                        break
                parent = parent.parent if parent else None

    # --- Strategy 3: React Native Web / New Layout Anchor ---
    if not comment_boxes:
        certified_elems = product_html.find_all(string=re.compile("Certified Buyer"))
        for elem in certified_elems:
            try:
                parent = elem
                for _ in range(6):
                    parent = parent.parent
                if parent and parent.name == 'div' and id(parent) not in seen:
                    seen.add(id(parent))
                    comment_boxes.append(parent)
            except Exception:
                pass

    # --- Extract fields from each box ---
    reviews = []
    for comment_box in comment_boxes:
        try:
            box_text_parts = [p.strip() for p in comment_box.get_text(separator='|||', strip=True).split('|||') if p.strip()]
            full_text = ' | '.join(box_text_parts)
            is_rnw = 'Certified Buyer' in full_text

            # --- Name ---
            name = 'Anonymous'
            for cls in ['_2NsDsF AwS1CA', '_2NsDsF', 'r-1h0z5md', 'css-146c3p1']:
                elems = comment_box.find_all(['p', 'span', 'div'], class_=cls)
                for e in elems:
                    t = e.get_text(strip=True)
                    if t and len(t) > 2 and 'Certified' not in t and not t.isdigit() and len(t) < 40:
                        name = t
                        break
                if name != 'Anonymous': break
            
            if name == 'Anonymous' and is_rnw:
                try:
                    cb_idx = next(i for i, p in enumerate(box_text_parts) if 'Certified Buyer' in p)
                    if cb_idx >= 2:
                        candidate = box_text_parts[cb_idx - 2]
                        if len(candidate) > 1 and not re.match(r'^\d', candidate):
                            name = candidate
                except Exception: pass

            # --- Rating ---
            rating = ''
            # Try common Flipkart rating classes
            for cls in ['_3LWZlK', 'XQDdHH', 'W_S96H']:
                elems = comment_box.find_all(['div', 'span'], class_=cls)
                if elems:
                    r = elems[0].get_text(strip=True)
                    if r and r[0].isdigit():
                        rating = r[0]
                        break
            
            # Fallback for RNW: Rating is often a child div with text 1-5 near a star
            if not rating:
                for part in box_text_parts[:10]: # Look in first few parts
                    if re.match(r'^[1-5]$', part):
                        rating = part
                        break
            
            if not rating:
                # Look for any element with only a digit
                digit_elem = comment_box.find(string=re.compile(r'^[1-5]$'))
                if digit_elem:
                    rating = digit_elem.strip()

            if not rating: rating = '5'

            # --- Comment Heading ---
            comment_head = ''
            for cls in ['_2-N8zT', 'z9E0IG']:
                elems = comment_box.find_all(['p', 'span'], class_=cls)
                if elems:
                    comment_head = elems[0].get_text(strip=True)
                    break
            if not comment_head and box_text_parts:
                if len(box_text_parts[0]) < 60 and len(box_text_parts[0]) > 3:
                    comment_head = box_text_parts[0]

            # --- Comment Body ---
            comment = ''
            for cls in ['t-ZTKy', 'ZmyHeo']:
                elems = comment_box.find_all('div', class_=cls)
                if elems:
                    comment = elems[0].get_text(separator=' ', strip=True)
                    break
            if not comment:
                candidates = [p for p in box_text_parts if 'Certified Buyer' not in p and len(p) > 20]
                if candidates:
                    comment = max(candidates, key=len)

            # Clean
            for pattern in [r'READ MORE', r'Certified Buyer', r'Permalink', r'Report Abuse', r'Certified Purchaser']:
                comment = re.sub(pattern, '', comment, flags=re.IGNORECASE)
            comment = re.sub(r'\s+', ' ', comment).strip()

            if not comment or len(comment) < 5: continue

            # Robust deduplication by comparing alphanumeric content
            norm_comment = re.sub(r'\W+', '', comment).lower()
            is_duplicate = False
            for r in reviews:
                if re.sub(r'\W+', '', r["Comment"]).lower() == norm_comment:
                    is_duplicate = True
                    break
            
            if is_duplicate:
                continue

            reviews.append({
                "Name": name,
                "Rating": rating,
                "CommentHead": comment_head,
                "Comment": comment,
                "Images": [],
                "Upvotes": '0',
                "Downvotes": '0',
                "Location": '',
                "Date": '',
                "Config": ''
            })

        except Exception as e:
            continue

    return reviews


@app.route("/review", methods=['GET', 'POST'])
def review():
    """Handle product review search and display results."""
    product_name = request.args.get('product_name') or request.form.get('product_name') or \
                   request.args.get('content') or request.form.get('content')

    if not product_name:
        return render_template('results.html', product_name=None, reviews=[], error="Please enter a product name.")

    driver = None
    try:
        # --- Launch Chrome ---
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        driver_path = ChromeDriverManager().install()
        driver = webdriver.Chrome(service=Service(driver_path), options=options)
        
        # Search
        driver.get(f"https://www.flipkart.com/search?q={urllib.parse.quote(product_name)}")
        time.sleep(2)

        # First product
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/p/']")
        if not links:
            return render_template('results.html', product_name=product_name, reviews=[], error=f"No products found for '{product_name}'.")

        p_url = links[0].get_attribute('href')
        driver.get(p_url)
        time.sleep(3)

        # Scroll deeply to ensure reviews load (especially on React layouts)
        for y in [1000, 2000, 3000, 4000]:
            driver.execute_script(f"window.scrollTo(0, {y});")
            time.sleep(0.5)
        
        # Try to find "All reviews" link and click if possible, or just stay here
        try:
            all_reviews_link = driver.find_element(By.XPATH, "//a[contains(@href, '/product-reviews/')]")
            driver.get(all_reviews_link.get_attribute('href'))
            time.sleep(3)
            driver.execute_script("window.scrollTo(0, 1000);")
            time.sleep(1)
        except:
            pass

        # Get Page
        product_html = bs(driver.page_source, 'html.parser')
        
        # Get Overall Rating & Name
        product_rating = None
        for cls in ['_3LWZlK', 'XQDdHH']:
            elems = product_html.find_all(['div', 'span'], class_=cls)
            if elems:
                try:
                    product_rating = float(elems[0].get_text(strip=True))
                    break
                except: pass

        # Parse
        reviews = parse_reviews_from_html(product_html)

        if not reviews:
            return render_template('results.html', product_name=product_name, product_rating=product_rating, reviews=[], error=f"No reviews found for '{product_name}'.")

        return render_template('results.html', product_name=product_name, product_rating=product_rating, reviews=reviews)

    except Exception as e:
        return render_template('results.html', product_name=None, reviews=[], error=f"Error: {str(e)}")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass


if __name__ == "__main__":
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
