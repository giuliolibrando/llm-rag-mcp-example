#!/usr/bin/env python3
"""
AnythingLLM Web Uploader

This script uploads markdown files to AnythingLLM using web automation
since the REST API doesn't support document upload.
"""

import os
import time
import pathlib
from typing import List, Optional
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Configuration
ANYL_BASE_URL = os.environ.get('ANYL_BASE_URL', 'http://anythingai.xdkr-pai1.amm.dom.uniroma1.it')
ANYL_API_KEY = os.environ.get('ANYL_API_KEY', '')
ANYL_WORKSPACE = os.environ.get('ANYL_WORKSPACE', 'infosapienza')
OUTDIR = pathlib.Path('/app/out_md')

def log(msg: str) -> None:
    """Log message with timestamp."""
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}")

def setup_driver() -> webdriver.Chrome:
    """Setup Chrome driver with headless options."""
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36')
    
    driver = webdriver.Chrome(options=chrome_options)
    driver.implicitly_wait(10)
    return driver

def login_to_anythingllm(driver: webdriver.Chrome) -> bool:
    """Login to AnythingLLM using API key."""
    try:
        log("Navigating to AnythingLLM...")
        driver.get(ANYL_BASE_URL)
        
        # Wait for page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        
        # Check if we need to login
        try:
            # Look for login form or API key input
            api_key_input = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password'], input[placeholder*='API'], input[placeholder*='key']"))
            )
            
            log("Found API key input, entering credentials...")
            api_key_input.clear()
            api_key_input.send_keys(ANYL_API_KEY)
            
            # Look for submit button
            submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], button:contains('Login'), button:contains('Submit')")
            submit_btn.click()
            
            # Wait for login to complete
            time.sleep(3)
            
        except TimeoutException:
            log("No login form found, might already be authenticated")
        
        return True
        
    except Exception as e:
        log(f"Login failed: {e}")
        return False

def navigate_to_workspace(driver: webdriver.Chrome) -> bool:
    """Navigate to the specific workspace."""
    try:
        log(f"Navigating to workspace: {ANYL_WORKSPACE}")
        
        # Try to find workspace selector or direct navigation
        workspace_url = f"{ANYL_BASE_URL}/workspace/{ANYL_WORKSPACE}"
        driver.get(workspace_url)
        
        # Wait for workspace to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        
        time.sleep(2)
        return True
        
    except Exception as e:
        log(f"Failed to navigate to workspace: {e}")
        return False

def upload_files_to_workspace(driver: webdriver.Chrome, file_paths: List[pathlib.Path]) -> int:
    """Upload files to the workspace."""
    uploaded_count = 0
    
    try:
        # Look for upload button or file input
        upload_selectors = [
            "input[type='file']",
            "button:contains('Upload')",
            "button:contains('Add')",
            "button:contains('Import')",
            "[data-testid*='upload']",
            ".upload-button",
            "#upload-button"
        ]
        
        upload_element = None
        for selector in upload_selectors:
            try:
                upload_element = driver.find_element(By.CSS_SELECTOR, selector)
                log(f"Found upload element with selector: {selector}")
                break
            except NoSuchElementException:
                continue
        
        if not upload_element:
            log("Could not find upload element")
            return 0
        
        # If it's a file input, upload files directly
        if upload_element.tag_name == 'input' and upload_element.get_attribute('type') == 'file':
            log(f"Found file input, uploading {len(file_paths)} files...")
            
            # Convert paths to strings for Selenium
            file_paths_str = [str(fp) for fp in file_paths]
            upload_element.send_keys('\n'.join(file_paths_str))
            
            # Wait for upload to complete
            time.sleep(5)
            uploaded_count = len(file_paths)
            
        else:
            # If it's a button, click it and then upload
            log("Found upload button, clicking...")
            upload_element.click()
            
            # Wait for file dialog or upload interface
            time.sleep(2)
            
            # Look for file input that appeared
            file_input = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
            )
            
            # Upload files
            file_paths_str = [str(fp) for fp in file_paths]
            file_input.send_keys('\n'.join(file_paths_str))
            
            # Wait for upload to complete
            time.sleep(5)
            uploaded_count = len(file_paths)
        
        log(f"Successfully uploaded {uploaded_count} files")
        return uploaded_count
        
    except Exception as e:
        log(f"Upload failed: {e}")
        return 0

def get_markdown_files() -> List[pathlib.Path]:
    """Get all markdown files from output directories."""
    files = []
    
    # Get files from issues directory
    issues_dir = OUTDIR / 'issues'
    if issues_dir.exists():
        files.extend(issues_dir.glob('*.md'))
    
    # Get files from wiki directory
    wiki_dir = OUTDIR / 'wiki'
    if wiki_dir.exists():
        files.extend(wiki_dir.glob('*.md'))
    
    # Get files from wikijs directory
    wikijs_dir = OUTDIR / 'wikijs'
    if wikijs_dir.exists():
        files.extend(wikijs_dir.glob('*.md'))
    
    log(f"Found {len(files)} markdown files to upload")
    return files

def main():
    """Main function to upload files to AnythingLLM."""
    if not ANYL_API_KEY:
        log("Error: ANYL_API_KEY not set")
        return 1
    
    # Get markdown files
    files = get_markdown_files()
    if not files:
        log("No markdown files found to upload")
        return 0
    
    # Limit to first 10 files for testing
    files = files[:10]
    log(f"Uploading {len(files)} files (limited for testing)")
    
    driver = None
    try:
        # Setup driver
        log("Setting up Chrome driver...")
        driver = setup_driver()
        
        # Login
        if not login_to_anythingllm(driver):
            log("Failed to login to AnythingLLM")
            return 1
        
        # Navigate to workspace
        if not navigate_to_workspace(driver):
            log("Failed to navigate to workspace")
            return 1
        
        # Upload files
        uploaded = upload_files_to_workspace(driver, files)
        
        if uploaded > 0:
            log(f"Successfully uploaded {uploaded} files to AnythingLLM")
        else:
            log("No files were uploaded")
        
        return 0 if uploaded > 0 else 1
        
    except Exception as e:
        log(f"Error: {e}")
        return 1
        
    finally:
        if driver:
            driver.quit()

if __name__ == "__main__":
    exit(main())
