import requests
from bs4 import BeautifulSoup
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from azure.storage.blob import BlobServiceClient, BlobClient, ContentSettings
import json
import uuid
import os
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime

# Azure Storage Account configuration
AZURE_STORAGE_ACCOUNT_CONNECTION_STRING = os.getenv("AZURE_STORAGE_ACCOUNT_CONNECTION_STRING")
AZURE_STORAGE_ACCOUNT_CONTAINER_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_CONTAINER_NAME")

# List of URLs to scrape
urls_to_scrape = [
        "https://www.alintaenergy.com.au/help-and-support/help-and-support/billing-and-pricing/how-to-read-your-bill", 
"https://www.alintaenergy.com.au/help-and-support/energy-support-and-tips/energy-saving-tips",
"https://www.alintaenergy.com.au/help-and-support/help-and-support/meters-and-connections/self-meter-read",
"https://www.alintaenergy.com.au/help-and-support/help-and-support/billing-and-pricing/hardship-and-financial-support",
"https://www.alintaenergy.com.au/help-and-support/help-and-support/customer-support/faults-and-emergencies/emergency-information/government-assistance-available",
"https://www.alintaenergy.com.au/help-and-support/help-and-support/customer-support/family-violence-support",
"https://www.alintaenergy.com.au/residential/billing-and-pricing/pay-your-bill",
"https://www.alintaenergy.com.au/help-and-support/energy-support-and-tips/energy-saving-tips.html",
"https://www.alintaenergy.com.au/help-and-support/help-and-support/meters-and-connections/smart-meters",
"https://www.alintaenergy.com.au/moving/moving-home"
    ]

def setup_selenium():
    """Setup and return a Selenium WebDriver with headless Chrome"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    driver = webdriver.Chrome(options=chrome_options)
    return driver

def expand_accordions(driver):
    """Find and expand all accordion elements on the page with improved handling for stale elements"""
    try:
        # This is a general approach - you may need to adjust selectors based on the actual website
        accordion_selectors = [
            ".accordion", 
            ".expandable", 
            ".collapse",
            "[data-toggle='collapse']",
            ".accordion-toggle",
            ".accordion__button"
        ]
        
        for selector in accordion_selectors:
            # Instead of getting all elements at once, we'll find and click them one by one
            # This helps avoid stale element references when DOM changes after clicks
            try:
                # Find all matching elements for this selector
                accordion_elements = driver.find_elements(By.CSS_SELECTOR, selector)
                count = len(accordion_elements)
                
                # If we found elements, process them individually
                if count > 0:
                    print(f"Found {count} potential accordion elements with selector: {selector}")
                    
                    # Process each index instead of each element to avoid stale references
                    for i in range(count):
                        try:
                            # Re-find the element each time to avoid stale references
                            elements = driver.find_elements(By.CSS_SELECTOR, selector)
                            if i < len(elements):  # Make sure the index is still valid
                                element = elements[i]
                                if element.is_displayed() and element.is_enabled():
                                    # Use JavaScript click which is more reliable for this purpose
                                    driver.execute_script("arguments[0].click();", element)
                                    print(f"Clicked accordion element {i+1}/{count}")
                                    # Wait for DOM to update after click
                                    time.sleep(1)
                        except Exception as e:
                            print(f"Error processing accordion element {i+1}: {e}")
                            # Continue to the next element
            except Exception as e:
                print(f"Error with selector {selector}: {e}")
                # Continue to the next selector
                    
        # Additional approach: try using direct JavaScript to expand common accordion patterns
        try:
            # Try to expand Bootstrap accordions
            driver.execute_script("""
                // Try to expand Bootstrap accordions
                document.querySelectorAll('.collapse').forEach(function(el) {
                    el.classList.add('show');
                });
                
                // Try to expand by clicking all buttons that might control accordions
                document.querySelectorAll('button[aria-expanded="false"]').forEach(function(btn) {
                    btn.click();
                });
            """)
        except Exception as e:
            print(f"Error in JavaScript accordion expansion: {e}")
            
        # Wait for any animations to complete
        time.sleep(2)
    except Exception as e:
        print(f"Error in expand_accordions: {e}")

def scrape_url(url, driver):
    """Scrape content from a URL including expanding accordions"""
    try:
        driver.get(url)
        
        # Wait for page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        
        # Expand accordions
        expand_accordions(driver)
        
        # Get the page source after expanding accordions
        page_source = driver.page_source
        
        # Parse with BeautifulSoup
        soup = BeautifulSoup(page_source, 'html.parser')
        
        # Extract text content (you can modify this to extract specific elements)
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.extract()
            
        # Get text
        text = soup.get_text(separator='\n', strip=True)
        
        # Optional: Extract additional structured data if needed
        # For example, get all headings
        headings = [h.text.strip() for h in soup.find_all(['h1', 'h2', 'h3', 'h4'])]
        
        # Build content object
        content = {
            "url": url,
            "scraped_at": datetime.now().isoformat(),
            "full_text": text,
            "headings": headings
        }
        
        return content
        
    except Exception as e:
        print(f"Error scraping URL {url}: {e}")
        return None

def upload_to_azure(content, url):
    """Upload the scraped content to Azure Blob Storage"""
    try:
        # Initialize BlobServiceClient
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_ACCOUNT_CONNECTION_STRING)
        
        # Get container client
        container_client = blob_service_client.get_container_client(AZURE_STORAGE_ACCOUNT_CONTAINER_NAME)
        
        # Create a unique blob name
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        safe_url = url.replace("://", "_").replace("/", "_").replace(".", "_")
        blob_name = f"scrape_{safe_url}_{timestamp}.json"
        
        # Convert content to JSON string
        content_str = json.dumps(content, ensure_ascii=False, indent=2)
        
        # Create blob client
        blob_client = container_client.get_blob_client(blob_name)
        
        # Set metadata
        metadata = {
            "source_url": url,
            "scraped_at": datetime.now().isoformat()
        }
        
        # Upload content with metadata
        blob_client.upload_blob(
            content_str,
            metadata=metadata,
            content_settings=ContentSettings(content_type="application/json")
        )
        
        print(f"Uploaded content from {url} to blob: {blob_name}")
        return True
        
    except Exception as e:
        print(f"Error uploading to Azure: {e}")
        return False

def main():
    # Setup Selenium
    driver = setup_selenium()
    
    try:
        for url in urls_to_scrape:
            print(f"Scraping {url}...")
            content = scrape_url(url, driver)
            
            if content:
                # Upload to Azure
                upload_to_azure(content, url)
            
            # Be nice to servers - add delay between requests
            time.sleep(2)
            
    finally:
        # Clean up
        driver.quit()
        print("Scraping completed")

if __name__ == "__main__":
    main()